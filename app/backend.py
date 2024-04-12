import os
import json
import time
import logging
import requests
import numpy as np
import azure.functions as func
from utils import timer, count_tokens


# Cost table per 1000 tokens. Format is [input cost, output cost]
LLM_RATES = {
    'gpt35_4k': [0.0021, 0.003], 
    'gpt35_16k': [0.0007, 0.0021], 
    'gpt4_1106': [0.041, 0.082]
}

EMBED_COST = 0.000136  # per 1000 tokens


class rag():
    """Backend processor for the RAG engine"""

    def __init__(self, req: func.HttpRequest, model: str) -> None:
        """Extracting model parameters from the request object
        
        Args: 
            body                : the prompt
            model               : which model to use (ex gpt, llama, mistral)
            use_rag             : whether to pass extra context or to just use the prompt
            temperature         : randomness (higher = more)
            top_p               : top % of most similar tokens to sample from
            do_sample           : sample from a distribution of words, or just use the most likely word
            frequency_penalty   : incremental penalty based on frequency of token use
            presence_penalty    : flat penalty each time a repeated token is used
            max_new_tokens      : max tokens to produce in the response
            chunk_limit         : limit the size of each chunk passed to the llm (mostly for large chunk sizes)  
            k                   : how many chunks to pass to the llm as context

        """
        self.body = req.params.get('body')

        if not self.body:
            try:
                req = req.get_json()
            except ValueError:
                pass
            else:
                self.body = req.get('body')

        self.model = model
        self.use_rag = req.params.get('use_rag') in ["True", True, None]
        self.temperature = float(req.params.get('temperature') or 0.9)
        self.top_p = float(req.params.get('top_p') or 0.9)
        self.do_sample = req.params.get('do_sample') in ["True", True, None]
        self.frequency_penalty = float(req.params.get('frequency_penalty') or 0)
        self.presence_penalty = float(req.params.get('presence_penalty') or 0) 
        self.max_new_tokens = int(req.params.get('max_new_tokens') or 200)
        self.chunk_limit = int(req.params.get('chunk_limit') or 150)
        self.k = int(req.params.get('k') or 3) 

    def generate(self, data, index) -> func.HttpResponse:
        """Main method; coordinates other methods
        
        Args:
            data    : pd.DataFrame containing all the chunks
            index   : FAISS index containing embeddings for the chunks

        """
        if self.body:
            embedding, embed_time = self._embed()
            (scores, ids), search_time = self._index(index, embedding)
            entity, entity_extraction_time = self._entity_extractor()
            relevant_data = data.iloc[ids[0]]  # the most relevant chunks
            context = ' | '.join(relevant_data['chunks'][0:self.chunk_limit])  # joining the relevant chunks into a single string
            self.prompt = f"Answer: {self.body} {('using: ' + context) if self.use_rag else ''}"  # adding the chunks to the prompt
            response, generate_time = self._augment()

            logging.info("Response: %s", response)

            # Cost calculation:
            embed_tokens = count_tokens(self.body, 'text-embedding-ada-002')
            embed_cost = embed_tokens / 1000 * EMBED_COST
            llm_tokens_in = count_tokens(self.prompt, 'gpt-3.5-turbo')
            llm_tokens_out = count_tokens(response, 'gpt-3.5-turbo')
            llm_cost = "N/A" if not LLM_RATES.get(self.model) else np.dot(LLM_RATES.get(self.model), [llm_tokens_in/1000, llm_tokens_out/1000])

            return func.HttpResponse(
                json.dumps({
                    "id": round(time.time() * 1e3),
                    "response": response,
                    "sources": relevant_data.assign(similarity=scores[0])[['title', 'similarity', 'url', 'chunks']].to_dict(orient='records'),
                    "parameters": self.__dict__,
                    "logs": {
                        "runtime": {
                            "embed": round(embed_time, 2),
                            "search": round(search_time, 2),
                            "entity_extraction": round(entity_extraction_time, 2),
                            "generate": round(generate_time, 2),
                            "total": round(embed_time + search_time + generate_time + entity_extraction_time, 2)
                        },
                        "tokens": {
                            "embed": {
                                "in": embed_tokens,
                                "out": len(embedding[0])
                            },
                            "llm": {
                                "in": llm_tokens_in,
                                "out": llm_tokens_out
                            },
                        },
                        "cost": {
                            "embed": embed_cost,
                            "llm": llm_cost,
                            "total": embed_cost + llm_cost
                        },
                        # "embedding": embedding,
                        "entity": entity
                    }
                }),
                mimetype="application/json"
            )
        else:
            return func.HttpResponse(
                "This HTTP triggered function executed successfully. Pass a body in the query string or in the request body for a personalized response.",
                status_code=200
            )

    @timer
    def _embed(self) -> tuple[list[float], float]:
        """Embedding the user prompt into a numeric vector representation for processing
        
        Returns:
            list[float] : an ada002 embedding of size 1536
            float       : function runtime in seconds 
        
        """
        try:
            embedding = requests.post(
                url = "https://ragnalysis.openai.azure.com/openai/deployments/ada_embedding/embeddings?api-version=2023-05-15", 
                headers = { "Content-Type": "application/json", "api-key": os.environ['OPENAI_KEY'] }, 
                json = { "input": self.body }
            ).json()['data'][0]['embedding']
        except KeyError as e:
            raise e('ADA embedding API failed to embed: %s', self.body) 
        else:
            return np.array([embedding], dtype='float32')

    @timer
    def _index(self, index, embedding) -> tuple[list[float], list[int], float]:
        """Searches the FAISS index using the prompt embedding for the most similar chunk embeddings 
        
        Returns:
            list[float]     : similarity scores of the retrieved chunks in the range [0, 1]
            list[int]       : indexes of the most relevant chunks in the pd.DataFrame containing the chunks in string form
            float           : function runtime in seconds

        """
        scores, ids = index.search(embedding, k=self.k)
        return scores, ids

    @timer
    def _entity_extractor(self) -> tuple[str, float]:
        """TODO: Entity extraction function for extracting entities from user prompts"""

        return ""

    @timer
    def _augment(self) -> tuple[str, float]:
        """Parent function for choosing which LLM to prompt for a response"""

        if self.model in ['llama', 'mistral']:
            response = self._ml_studio_model()
        elif self.model in ['gpt35_4k', 'gpt35_16k', 'gpt4_1106']:
            response = self._ai_studio_model()
        elif self.model in ['qwen']:
            response = self._containerized_model()
        else:
            raise Exception("Invalid model choice")
        return response

    def _ml_studio_model(self) -> str:
        """Child function (1/3) of _augment() that routes to the ML Studio models"""

        model = self.model.upper()
        endpoint = os.environ[f'{model}_ENDPOINT']
        try:
            response = requests.post(
                url = f'https://ragnalysis-{endpoint}.eastus2.inference.ml.azure.com/score',
                headers = {
                    'Content-Type':'application/json',
                    'Authorization':('Bearer '+ os.environ[f'{model}_KEY']), 
                    'azureml-model-deployment': os.environ[f'{model}_MODEL']
                },
                json = {
                    "input_data": {
                        "input_string": [{
                            "role": "user",
                            "content": self.prompt
                        }],
                        "parameters": {
                            "temperature": self.temperature,
                            "top_p": self.top_p,
                            "do_sample": self.do_sample,
                            "max_new_tokens": self.max_new_tokens
                        }
                    }
                }
            ).json()
            return response.get('output')
        except KeyError as e:
            raise e(f'ML studio model failed to generate prompt with the given context. \n \
                    Try setting use_rag to False to see if it is an issue with the context. \n \
                    Response object: {response}')

    def _ai_studio_model(self) -> str:
        """Child function (2/3) of _augment() that routes to the OpenAI Studio models"""
        try:
            response = requests.post(
                url = f"https://ragnalysis.openai.azure.com/openai/deployments/{self.model}/chat/completions?api-version=2023-05-15", 
                headers = { "Content-Type": "application/json", "api-key": os.environ['OPENAI_KEY'] }, 
                json = { 
                    "messages": [{
                        "role": "user",
                        "content": self.prompt
                    }],
                    "temperature": self.temperature,
                    "top_p": self.top_p,
                    "frequency_penalty": self.frequency_penalty,
                    "presence_penalty": self.presence_penalty,
                    "max_tokens": self.max_new_tokens,
                    "stop": None
                }
            ).json()
            return response['choices'][0]['message'].get('content')
        except KeyError as e:
            raise e(f'AI studio model failed to generate prompt with the given context. \n \
                    Try setting use_rag to False to see if it is an issue with the context. \n \
                    Response object: {response}')

    def _containerized_model(self) -> str:
        """Child function (3/3) of _augment() that routes to the containerized models"""
        try:
            return requests.post(
                url="https://localai-selfhost.salmonground-3deb4a95.canadaeast.azurecontainerapps.io/chat/completions",
                json={
                    "prompt": self.prompt,
                    "temperature": self.temperature,
                    "top_p": self.top_p,
                    "frequency_penalty": self.frequency_penalty,
                    "presence_penalty": self.presence_penalty,
                    "max_tokens": self.max_new_tokens,
                    "stop": None
                }
            ).json()['response']
        except KeyError as e:
            raise e(f'AI studio model failed to generate prompt with the given context. \n \
                    Try setting use_rag to False to see if it is an issue with the context. \n \
                    Response object: {response}')
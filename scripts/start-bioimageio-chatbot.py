import asyncio
import os
from imjoy_rpc.hypha import login, connect_to_server

from pydantic import BaseModel, Field
from schema_agents.role import Role
from schema_agents.schema import Message
from langchain.vectorstores import FAISS
from langchain.embeddings.openai import OpenAIEmbeddings
from typing import Any, Dict, List, Optional, Union
import requests
import sys
import io
import yaml
import json

MANIFEST = yaml.load(open("./manifest.yaml", "r"), Loader=yaml.FullLoader)
DOCS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs")
DB_PATH = os.path.join(DOCS_DIR, "knowledge-base")

def load_model_info():
    response = requests.get("https://bioimage-io.github.io/collection-bioimage-io/collection.json")
    assert response.status_code == 200
    model_info = response.json()
    resource_items = model_info['collection']
    return resource_items

def execute_code(script, context=None):
    if context is None:
        context = {}

    # Redirect stdout and stderr to capture their output
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    sys.stdout = io.StringIO()
    sys.stderr = io.StringIO()

    try:
        # Create a copy of the context to avoid modifying the original
        local_vars = context.copy()

        # Execute the provided Python script with access to context variables
        exec(script, local_vars)

        # Capture the output from stdout and stderr
        stdout_output = sys.stdout.getvalue()
        stderr_output = sys.stderr.getvalue()

        return {
            "stdout": stdout_output,
            "stderr": stderr_output,
            # "context": local_vars  # Include context variables in the result
        }
    except Exception as e:
        return {
            "stdout": "",
            "stderr": str(e),
            # "context": context  # Include context variables in the result even if an error occurs
        }
    finally:
        # Restore the original stdout and stderr
        sys.stdout = original_stdout
        sys.stderr = original_stderr


class ModelZooInfoScriptResults(BaseModel):
    """Results of executing the model zoo info query script."""
    stdout: str = Field(description="The output from stdout.")
    stderr: str = Field(description="The output from stderr.")
    request: str = Field(description="User's request in details")
    user_info: str = Field(description="User info for personalize response.")

class DirectResponse(BaseModel):
    """Direct response to a user's question."""
    response: str = Field(description="The response to the user's question.")

class DocumentSearchInput(BaseModel):
    """Results of document retrieval from documentation."""
    user_question: str = Field(description="The user's original question.")
    relevant_context: List[str] = Field(description="Context chunks from the documentation (in markdown format), ordered by relevance.")
    user_info: str = Field(description="User info for personalize response.")


class FinalResponse(BaseModel):
    """The final response to the user's question."""
    response: str = Field(description="The answer to the user's question in markdown format. If the question isn't relevant, return 'I don't know'.")


class UserProfile(BaseModel):
    """The user's profile. This will be used to personalize the response."""
    name: str = Field(description="The user's name.", max_length=32)
    occupation: str = Field(description="The user's occupation. ", max_length=128)
    background: str = Field(description="The user's background. ", max_length=256)

class QuestionWithHistory(BaseModel):
    """The user's question, chat history and user's profile."""
    question: str = Field(description="The user's question.")
    chat_history: Optional[List[Dict[str, str]]] = Field(None, description="The chat history.")
    user_profile: Optional[UserProfile] = Field(None, description="The user's profile. You should use this to personalize the response based on the user's background and occupation.")
    channel_id: Optional[str] = Field(None, description="The channel id of the user's question. This is used to limit the search scope to a specific channel, None means all the channels.")

def create_customer_service():
    docs_store_dict = load_knowledge_base()
    resource_items = load_model_info()
    types = set()
    tags = set()
    for resource in resource_items:
        types.add(resource['type'])
        tags.update(resource['tags'])
    types = list(types)
    tags = list(tags)[:10]
    
    channels_info = "\n".join(f"""- `{collection['id']}`: {collection['description']}""" for collection in MANIFEST['collections'])
    resource_item_stats = f"""Each item contains the following fields: {list(resource_items[0].keys())}\nThe available resource types are: {types}\nSome example tags: {tags}\nHere is an example: {resource_items[0]}"""
    class DocumentRetrievalInput(BaseModel):
        """Input for finding relevant documents from database."""
        query: str = Field(description="Query used to retrieve related documents.")
        request: str = Field(description="User's request in details")
        user_info: str = Field(description="Brief user info summary for personalized response, including name, background etc.")
        database_id: str = Field(description=f"Select a database for information retrieval. The available databases are:\n{channels_info}")

    class ModelZooInfoScript(BaseModel):
        """Create a Python Script to get information about details of models, applications and datasets etc."""
        script: str = Field(description="The script to be executed, the script use a predefined local variable `resources` which contains a list of dictionaries with all the resources in the model zoo (including models, applications, datasets etc.), the response to the query should be printed to the stdout. Details about the `resources`:\n" + resource_item_stats)
        request: str = Field(description="User's request in details")
        user_info: str = Field(description="Brief user info summary for personalized response, including name, background etc.")

    async def respond_to_user(question_with_history: QuestionWithHistory = None, role: Role = None) -> str:
        """Answer the user's question directly or retrieve relevant documents from the documentation, or create a Python Script to get information about details of models."""
        inputs = [question_with_history.user_profile] + list(question_with_history.chat_history) + [question_with_history.question] 
        req = await role.aask(inputs, Union[DirectResponse, DocumentRetrievalInput, ModelZooInfoScript])
        if isinstance(req, DirectResponse):
            return req.response
        elif isinstance(req, DocumentRetrievalInput):
            # Use the automatic channel selection if the user doesn't specify a channel
            selected_channel = question_with_history.channel_id or req.database_id
            docs_store = docs_store_dict[selected_channel]
            relevant_docs = await docs_store.asimilarity_search(req.query, k=3)
            raw_docs = [doc.page_content for doc in relevant_docs]
            search_input = DocumentSearchInput(user_question=req.request, relevant_context=raw_docs, user_info=req.user_info)
            response = await role.aask(search_input, FinalResponse)
            return response.response
        elif isinstance(req, ModelZooInfoScript):
            loop = asyncio.get_running_loop()
            print(f"Executing the script:\n{req.script}")
            result = await loop.run_in_executor(None, execute_code, req.script, {"resources": resource_items})
            print(f"Script execution result:\n{result}")
            response = await role.aask(ModelZooInfoScriptResults(
                stdout=result["stdout"],
                stderr=result["stderr"],
                request=req.request,
                user_info=req.user_info
            ), FinalResponse)
            return response.response
        
    CustomerServiceRole = Role.create(
        name="Liza",
        profile="Customer Service",
        goal="You are a customer service representative for the BioImage.IO helpdesk. You will answer user's questions related bioimage analysis, ask for clarification, and retrieve documents from databases or executing scripts. You may also get user's profile to personalize the response in order to improve the user experience.",
        constraints=None,
        actions=[respond_to_user],
    )
    customer_service = CustomerServiceRole()
    return customer_service

def load_docs_store(db_path, collection_name):
    # Load from vector store
    embeddings = OpenAIEmbeddings()
    docs_store = FAISS.load_local(index_name=collection_name, folder_path=db_path, embeddings=embeddings)
    return docs_store

def load_knowledge_base():
    channel_ids = [collection['id'] for collection in MANIFEST['collections']]
    docs_store_dict = {channel: load_docs_store(DB_PATH, channel) for channel in channel_ids}
    for name, docs_store in docs_store_dict.items():
        length = len(docs_store.docstore._dict.keys())
        assert  length > 0, f"Please make sure the docs store {name} is not empty."
        print(f"Loaded {length} documents from {name}")

    return docs_store_dict

async def main():
    customer_service = create_customer_service()
    chat_history=[]

    question = "How can I segment an cell image?"
    profile = UserProfile(name="lulu", occupation="data scientist", background="machine learning and AI")
    m = QuestionWithHistory(question=question, chat_history=chat_history, user_profile=UserProfile.parse_obj(profile), channel_id="scikit-image")
    resp = await customer_service.handle(Message(content=m.json(), instruct_content=m , role="User"))

    question = "How can I test the models?"
    profile = UserProfile(name="lulu", occupation="data scientist", background="machine learning and AI")
    m = QuestionWithHistory(question=question, chat_history=chat_history, user_profile=UserProfile.parse_obj(profile), channel_id="bioimage.io")
    resp = await customer_service.handle(Message(content=m.json(), instruct_content=m , role="User"))

    question = "What are Model Contribution Guidelines?"
    m = QuestionWithHistory(question=question, chat_history=chat_history, user_profile=UserProfile.parse_obj(profile))
    resp = await customer_service.handle(Message(content=m.json(), instruct_content=m , role="User"))
    print(resp)
    # resp = await customer_service.handle(Message(content="What are Model Contribution Guidelines?", role="User"))


async def start_server(server_url):
    channel_id_by_name = {collection['name']: collection['id'] for collection in MANIFEST['collections']}
    token = await login({"server_url": server_url})
    server = await connect_to_server({"server_url": server_url, "token": token, "method_timeout": 100})
    customer_service = create_customer_service()

    async def chat(text, chat_history, user_profile=None, channel=None, context=None):
        # Get the channel id by its name
        if channel:
            assert channel in channel_id_by_name, f"Channel {channel} is not found, available channels are {list(channel_id_by_name.keys())}"
            channel_id = channel_id_by_name[channel]
        else:
            channel_id = None
        
        # user_profile = {"name": "lulu", "occupation": "data scientist", "background": "machine learning and AI"}
        m = QuestionWithHistory(question=text, chat_history=chat_history, user_profile=UserProfile.parse_obj(user_profile), channel_id=channel_id)
        response = await customer_service.handle(Message(content=m.json(), instruct_content=m , role="User"))
        # get the content of the last response
        response = response[-1].content
        print(f"\nUser: {text}\nBot: {response}")
        return response

    hypha_service_info = await server.register_service({
        "name": "Hypha Bot",
        "id": "hypha-bot",
        "config": {
            "visibility": "public",
            "require_context": True
        },
        "chat": chat,
        "channels": [collection['name'] for collection in MANIFEST['collections']]
    })
    
    async def index(event, context=None):
        with open(os.path.join(os.path.dirname(__file__), "index-template.html"), "r") as f:
            html = f.read()
        html = html.replace("{{ SERVICE_ID }}", hypha_service_info['id'])
        return {
            "status": 200,
            "headers": {'Content-Type': 'text/html'},
            "body": html
        }
    
    await server.register_service({
        "id": "hypha-bot-client",
        "type": "functions",
        "config": {
            "visibility": "public",
            "require_context": False
        },
        "index": index,
    })

    print(f"visit this to test the bot: {server_url}/{server.config['workspace']}/apps/hypha-bot-client/index")

if __name__ == "__main__":
    # asyncio.run(main())
    server_url = "https://ai.imjoy.io"
    loop = asyncio.get_event_loop()
    loop.create_task(start_server(server_url))
    loop.run_forever()
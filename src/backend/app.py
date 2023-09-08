import io
import logging
import mimetypes
import os
import time
import json
import jwt
import urllib.parse

import aiohttp
import openai

import aiohttp
import openai
from azure.identity.aio import DefaultAzureCredential
from azure.monitor.opentelemetry import configure_azure_monitor
from azure.search.documents.aio import SearchClient
from azure.storage.blob.aio import BlobServiceClient
from azure.cosmos.aio import CosmosClient, ContainerProxy
from opentelemetry.instrumentation.aiohttp_client import AioHttpClientInstrumentor
from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware
from quart import (
    Blueprint,
    Quart,
    abort,
    current_app,
    jsonify,
    request,
    send_file,
    send_from_directory,
)

from approaches.chatread import ChatReadApproach
from approaches.chatreadretrieveread import ChatReadRetrieveReadApproach

CONFIG_OPENAI_TOKEN = "openai_token"
CONFIG_CREDENTIAL = "azure_credential"
CONFIG_CHAT_APPROACHES = "chat_approaches"
CONFIG_DOCSEARCH_APPROACHES = "docsearch_approaches"
CONFIG_BLOB_CONTAINER_CLIENT = "blob_container_client"
CONFIG_AUTH_TOKEN = "X-MS-TOKEN-AAD-ID-TOKEN"

bp = Blueprint("routes", __name__, static_folder='static')

@bp.route("/")
async def index():
    return await bp.send_static_file("index.html")

@bp.route("/favicon.ico")
async def favicon():
    return await bp.send_static_file("favicon.ico")

@bp.route("/assets/<path:path>")
async def assets(path):
    return await send_from_directory("static/assets", path)

# Serve content files from blob storage from within the app to keep the example self-contained.
# *** NOTE *** this assumes that the content files are public, or at least that all users of the app
# can access all the files. This is also slow and memory hungry.
@bp.route("/content/<path>")
async def content_file(path):
    try:
        path = path.strip()
        blob_container_client = current_app.config[CONFIG_BLOB_CONTAINER_CLIENT]
        blob = blob_container_client.get_blob_client(blob=path)
        properties = await blob.get_blob_properties()
        if properties.size < 1024 * 1024: # 1MB
            blob = await blob_container_client.get_blob_client(path).download_blob()

            if not blob.properties or not blob.properties.has_key("content_settings"):
                abort(404)
            mime_type = blob.properties["content_settings"]["content_type"]

            if mime_type == "application/octet-stream":
                mime_type = mimetypes.guess_type(path)[0] or "application/octet-stream"

            _, ext = os.path.splitext(path)
            extensions = ["doc", "docs", "xls", "xlsx", "ppt", "pptx"]
            as_attachment = ext[1:].lower() in extensions

            blob_file = io.BytesIO()
            await blob.readinto(blob_file)
            blob_file.seek(0)
            return await send_file(blob_file, mimetype=mime_type, as_attachment=as_attachment, attachment_filename=urllib.parse.quote(path))
        else:
            html = f"<!DOCTYPE html><html><head><title>oversize file</title></head><body><p>Subject file cannot be previewed due to the size limit, {properties.size} bytes. See [Supporting content] tab.</p></body></html>"
            return html, 403, {"Content-Type": "text/html"}
    except Exception as e:
        user_name = get_user_name(request)
        write_error("content", user_name, str(e))
        return jsonify({"error": str(e)}), 500

@bp.route("/chat", methods=["POST"])
async def chat():
    if not request.is_json:
        return jsonify({"error": "request must be json"}), 415
    request_json = await request.get_json()
    approach = request_json.get("approach")
    user_name = get_user_name(request)
    try:
        impl = current_app.config[CONFIG_CHAT_APPROACHES].get(approach)
        if not impl:
            return jsonify({"error": "unknown approach"}), 400
        # Workaround for: https://github.com/openai/openai-python/issues/371
        async with aiohttp.ClientSession() as s:
            openai.aiosession.set(s)
            r = await impl.run(user_name, request_json["history"], request_json.get("overrides"))
        return jsonify(r)
    except Exception as e:
        write_error("chat", user_name, str(e))
        return jsonify({"error": str(e)}), 500


@bp.route("/docsearch", methods=["POST"])
async def docsearch():
    if not request.is_json:
        return jsonify({"error": "request must be json"}), 415
    request_json = await request.get_json()
    approach = request_json["approach"]
    user_name = get_user_name(request)
    try:
        impl = current_app.config[CONFIG_DOCSEARCH_APPROACHES].get(approach)
        if not impl:
            return jsonify({"error": "unknown approach"}), 400
        # Workaround for: https://github.com/openai/openai-python/issues/371
        async with aiohttp.ClientSession() as s:
            openai.aiosession.set(s)
            r = await impl.run(user_name, request_json["history"], request_json.get("overrides"))
        return jsonify(r)
    except Exception as e:
        write_error("docsearch", user_name, str(e))
        return jsonify({"error": str(e)}), 500

@bp.before_request
async def ensure_openai_token():
    openai_token = current_app.config[CONFIG_OPENAI_TOKEN]
    if openai_token.expires_on < time.time() + 60:
        openai_token = await current_app.config[CONFIG_CREDENTIAL].get_token("https://cognitiveservices.azure.com/.default")
        current_app.config[CONFIG_OPENAI_TOKEN] = openai_token
        openai.api_key = openai_token.token

@bp.before_app_serving
async def setup_clients():

    # Replace these with your own values, either in environment variables or directly here
    AZURE_STORAGE_ACCOUNT = os.getenv("AZURE_STORAGE_ACCOUNT")
    AZURE_STORAGE_CONTAINER = os.getenv("AZURE_STORAGE_CONTAINER")
    AZURE_SEARCH_SERVICE = os.getenv("AZURE_SEARCH_SERVICE")
    AZURE_SEARCH_INDEX = os.getenv("AZURE_SEARCH_INDEX")
    AZURE_OPENAI_SERVICE = os.getenv("AZURE_OPENAI_SERVICE")
    AZURE_OPENAI_EMB_DEPLOYMENT = os.getenv("AZURE_OPENAI_EMB_DEPLOYMENT")

    KB_FIELDS_CONTENT = os.getenv("KB_FIELDS_CONTENT", "content")
    KB_FIELDS_SOURCEPAGE = os.getenv("KB_FIELDS_SOURCEPAGE", "sourcepage")

    # Use the current user identity to authenticate with Azure OpenAI, Cognitive Search and Blob Storage (no secrets needed,
    # just use 'az login' locally, and managed identity when deployed on Azure). If you need to use keys, use separate AzureKeyCredential instances with the
    # keys for each service
    # If you encounter a blocking error during a DefaultAzureCredential resolution, you can exclude the problematic credential by using a parameter (ex. exclude_shared_token_cache_credential=True)
    azure_credential = DefaultAzureCredential()
    # azure_credential = DefaultAzureCredential(exclude_shared_token_cache_credential = True)

    # Set up clients for Cognitive Search, Storage and CosmosDB
    search_client = SearchClient(
            endpoint=f"https://{AZURE_SEARCH_SERVICE}.search.windows.net",
            index_name=AZURE_SEARCH_INDEX,
            credential=azure_credential)
    blob_client = BlobServiceClient(
        account_url=f"https://{AZURE_STORAGE_ACCOUNT}.blob.core.windows.net",
        credential=azure_credential)
    blob_container_client = blob_client.get_container_client(AZURE_STORAGE_CONTAINER)

    # CosmosDB
    AZURE_COSMOSDB_ENDPOINT = os.environ.get("AZURE_COSMOSDB_ENDPOINT")
    AZURE_COSMOSDB_DATABASE = os.environ.get("AZURE_COSMOSDB_DATABASE")
    AZURE_COSMOSDB_CONTAINER = os.environ.get("AZURE_COSMOSDB_CONTAINER")

    cosmosdb_client = CosmosClient(AZURE_COSMOSDB_ENDPOINT, azure_credential)
    cosmosdb_database = cosmosdb_client.get_database_client(AZURE_COSMOSDB_DATABASE)
    cosmosdb_container = cosmosdb_database.get_container_client(AZURE_COSMOSDB_CONTAINER)

    # Used by the OpenAI SDK
    openai.api_base = f"https://{AZURE_OPENAI_SERVICE}.openai.azure.com"
    openai.api_version = "2023-05-15"
    openai.api_type = "azure_ad"
    openai_token = await azure_credential.get_token(
        "https://cognitiveservices.azure.com/.default"
    )
    openai.api_key = openai_token.token

    # Store on app.config for later use inside requests
    current_app.config[CONFIG_OPENAI_TOKEN] = openai_token
    current_app.config[CONFIG_CREDENTIAL] = azure_credential
    current_app.config[CONFIG_BLOB_CONTAINER_CLIENT] = blob_container_client

    # Various approaches to integrate GPT and external knowledge, most applications will use a single one of these patterns
    # or some derivative, here we include several for exploration purposes
    current_app.config[CONFIG_CHAT_APPROACHES] = {
        "chat": ChatReadApproach(
            cosmosdb_container
        )
    }
    current_app.config[CONFIG_DOCSEARCH_APPROACHES] = {
        "docsearch": ChatReadRetrieveReadApproach(
            search_client,
            cosmosdb_container, 
            AZURE_OPENAI_EMB_DEPLOYMENT,
            KB_FIELDS_SOURCEPAGE,
            KB_FIELDS_CONTENT,
        )
    }

def get_user_name(req: request):
    user_name = ""

    if CONFIG_AUTH_TOKEN in req.headers:
        token = req.headers[CONFIG_AUTH_TOKEN]
        claim = jwt.decode(jwt=token, options={"verify_signature": False})
        user_name = claim["preferred_username"]
    else:
        user_name = "anonymous"

    return user_name

def write_error(category: str, user_name: str, error: str):
    properties = {
        "category" : category, # "chat", "docsearch", "content"
        "user" : user_name,
        "error" : error
    }

    logging.exception(json.dumps(properties).encode('utf-8').decode('unicode-escape'))

def create_app():
    if os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING"):
        configure_azure_monitor()
        AioHttpClientInstrumentor().instrument()
    app = Quart(__name__)
    app.register_blueprint(bp)
    app.asgi_app = OpenTelemetryMiddleware(app.asgi_app)

    return app
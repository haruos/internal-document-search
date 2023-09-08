from azure.cosmos import ContainerProxy

from enum import Enum

class ApproachType(Enum):
    Chat = "chat"
    DocSearch = "docsearch"

async def write_chatlog(container: ContainerProxy, approach: ApproachType, user_name: str, total_tokens: int, input: str, response: str, query: str=""):
    properties = {
        "approach" : approach.value,
        "user" : user_name, 
        "tokens" : total_tokens,
        "input" : input,  
        "response" : response
    }

    if query != "":
        properties["query"] = query
        
    item = await container.create_item(body=properties, enable_automatic_id_generation=True)

    return item

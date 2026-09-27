from fastapi import FastAPI
from pydantic import BaseModel
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from sage.core.llm import LLMClient
from sage.core.conversation import Conversation
from sage.core.database import Database

app = FastAPI()


app.mount("/static", StaticFiles(directory="sage/web/static"), name="static")


@app.get("/")
def home():
    return FileResponse("sage/web/static/index.html")

llm = LLMClient()

db = Database()
db.initialize()

conversation_id = db.get_or_create_primary_conversation()


# conversation = Conversation()


class ChatRequest(BaseModel):
    message: str


@app.post("/chat")
def chat(request: ChatRequest):
    db.add_message(
        conversation_id,
        "user",
        request.message
    )

    messages = db.get_messages(conversation_id)

    response = llm.chat(messages)

    db.add_message(
        conversation_id,
        "assistant",
        response
    )

    return {
        "response": response
    }

import os, json, asyncio, traceback
from typing import List, Dict, Any
from fastapi import FastAPI, WebSocket
from fastapi.websockets import WebSocketDisconnect
from openai import AsyncOpenAI
from fastapi.responses import JSONResponse


BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.deepinfra.com/v1/openai")
API_KEY = os.getenv("OPENAI_API_KEY", "REPLACE_ME")
MODEL = os.getenv("MODEL_NAME", "meta-llama/Meta-Llama-3.1-8B-Instruct")

FUNCTIONS: List[Dict[str, Any]] = []  # remplace par ta liste quand tu es prêt

app = FastAPI()
client = AsyncOpenAI(api_key=API_KEY, base_url=BASE_URL)

@app.websocket("/llm-websocket")
async def websocket_handler(ws: WebSocket):
    await ws.accept()
    current_stream_id = -1

    async def stream_text_response(req_data: Dict[str, Any]):
        try:
            prompt = prepare_prompt(req_data)
            stream = await client.chat.completions.create(
                model=MODEL, messages=prompt, temperature=0.2, stream=True
            )
            async for chunk in stream:
                delta = chunk.choices[0].delta
                if delta and delta.content:
                    await ws.send_text(json.dumps({
                        "type": "stream_response",
                        "data": { "stream_id": req_data["stream_id"], "content": delta.content, "end_of_stream": False }
                    }))
                if req_data["stream_id"] < current_stream_id:
                    return
            await ws.send_text(json.dumps({
                "type": "stream_response",
                "data": { "stream_id": req_data["stream_id"], "content": "", "end_of_stream": True }
            }))
        except Exception:
            print(traceback.format_exc(), flush=True)
            await ws.send_text(json.dumps({
                "type": "stream_response",
                "data": { "stream_id": req_data["stream_id"], "content": "Désolé, incident technique.", "end_of_stream": True }
            }))

    async def handle_turn(req_data: Dict[str, Any]):
        try:
            prompt = prepare_prompt(req_data)
            completion = await client.chat.completions.create(
                model=MODEL, messages=prompt, temperature=0.2,
                tools=FUNCTIONS if FUNCTIONS else None,
                tool_choice="auto" if FUNCTIONS else "none",
                stream=False
            )
            choice = completion.choices[0]
            msg = choice.message
            tool_calls = getattr(msg, "tool_calls", None)

            if tool_calls:
                tc = tool_calls[0]
                await ws.send_text(json.dumps({
                    "type": "stream_response",
                    "data": {
                        "stream_id": req_data["stream_id"],
                        "function_call": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments
                        },
                        "end_of_stream": True
                    }
                }))
                return

            asyncio.create_task(stream_text_response(req_data))

        except Exception:
            print(traceback.format_exc(), flush=True)
            await ws.send_text(json.dumps({
                "type": "stream_response",
                "data": { "stream_id": req_data["stream_id"], "content": "Désolé, incident technique.", "end_of_stream": True }
            }))

    try:
        while True:
            raw = await ws.receive_text()
            req = json.loads(raw)
            if req["type"] == "start_call":
                current_stream_id = req["data"]["stream_id"]
                await ws.send_text(json.dumps({
                    "type": "stream_response",
                    "data": { "stream_id": current_stream_id, "content": "Bonjour, je vous écoute.", "end_of_stream": True }
                }))
            elif req["type"] == "stream_request":
                current_stream_id = req["data"]["stream_id"]
                asyncio.create_task(handle_turn(req["data"]))
    except WebSocketDisconnect:
        pass

def prepare_prompt(req_data: Dict[str, Any]) -> List[Dict[str, str]]:
    messages: List[Dict[str, str]] = [{
        "role": "system",
        "content": "Tu es une assistante téléphonique médicale. Une seule question à la fois. Réponses brèves. Français de Suisse. Prononce Ciubotariu comme Tchioubotariou."
    }]
    for turn in req_data.get("transcript") or []:
        role = "assistant" if turn.get("role") == "assistant" else "user"
        messages.append({ "role": role, "content": turn.get("content") or "" })
    return messages

@app.get("/health")
def health():
    return JSONResponse({"status": "ok"})

@app.get("/ping")
def ping():
    return {"pong": True}



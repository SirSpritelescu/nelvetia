import os, json, asyncio, traceback
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from openai import AsyncOpenAI, APIStatusError

BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.deepinfra.com/v1/openai")
API_KEY  = os.getenv("OPENAI_API_KEY", "")
MODEL    = os.getenv("MODEL_NAME", "openai/gpt-oss-120b")

app = FastAPI()

class Agent:
    def __init__(self):
        print(f"[INIT] Using {MODEL} via {BASE_URL}", flush=True)
        self.client = AsyncOpenAI(api_key=API_KEY, base_url=BASE_URL)

    def prepare_prompt(self, request: dict):
        sys = "You are a concise, polite phone assistant. Reply in French (Switzerland)."
        messages = [{"role": "system", "content": sys}]
        user_msg = request.get("content") or request.get("delta") or "Bonjour"
        messages.append({"role": "user", "content": user_msg})
        print(f"[PROMPT] {messages}", flush=True)
        return messages

    async def draft_response(self, request: dict):
        sid = request["stream_id"]
        try:
            print(f"[LLM] Streaming from {MODEL}", flush=True)
            stream = await self.client.chat.completions.create(
                model=MODEL,
                messages=self.prepare_prompt(request),
                temperature=0.2,
                stream=True,
            )

            async for chunk in stream:
                delta = chunk.choices[0].delta
                if delta and delta.content:
                    print(f"[LLM CHUNK] {delta.content}", flush=True)
                    yield {
                        "stream_id": sid,
                        "content": delta.content,
                        "end_of_stream": False,
                    }

            yield {
                "stream_id": sid,
                "content": "",
                "end_of_stream": True,
            }

        except APIStatusError as e:
            print(f"[LLM ERROR] {e.status_code} {getattr(e, 'response', None)}", flush=True)
            yield {
                "stream_id": sid,
                "content": f"Erreur LLM ({e.status_code})",
                "end_of_stream": True,
            }
        except Exception:
            print("[LLM CRASH]", traceback.format_exc(), flush=True)
            yield {
                "stream_id": sid,
                "content": "Incident technique",
                "end_of_stream": True,
            }

    async def websocket_handler(self, websocket: WebSocket):
        await websocket.accept()
        print("[WS] client connected", flush=True)

        try:
            while True:
                message = await websocket.receive_text()
                request = json.loads(message)
                print(f"[WS IN] {request}", flush=True)

                if request["type"] == "start_call":
                    sid = request["data"]['stream_id']
                    await websocket.send_text(json.dumps({
                        "type": "stream_response",
                        "data": {
                            "stream_id": sid,
                            "content": "How can I help you?",
                            "flush": True,
                            "end_of_stream": True,
                        }
                    }))
                    print(f"[WS OUT] Hello sent (sid {sid})", flush=True)

                elif request["type"] == "stream_request":
                    asyncio.create_task(self.stream_response(websocket, request["data"]))

        except WebSocketDisconnect:
            print("[WS] disconnected", flush=True)

    async def stream_response(self, websocket, request):
        async for event in self.draft_response(request):
            await websocket.send_text(json.dumps({
                "type": "stream_response",
                "data": {**event, "flush": True}
            }))
            print(f"[WS OUT] {event}", flush=True)

agent = Agent()

@app.websocket("/llm-websocket")
async def websocket_route(ws: WebSocket):
    await agent.websocket_handler(ws)

@app.get("/ping")
def ping():
    return {"pong": True}

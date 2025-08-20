import os, json, asyncio, traceback
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from openai import AsyncOpenAI, APIStatusError

# --- Config via variables d'env (Render > Environment) ---
BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.deepinfra.com/v1/openai")
API_KEY  = os.getenv("OPENAI_API_KEY", "EAk6LnTYRt2IwArvx9gPSU4Ryxi80wXC")
MODEL    = os.getenv("MODEL_NAME", "openai/gpt-oss-120b")  # ex: openai/gpt-oss-120b

app = FastAPI()

class Agent:
    def __init__(self):
        # Client OpenAI-compatible pointant sur DeepInfra
        self.client = AsyncOpenAI(api_key=API_KEY, base_url=BASE_URL)

    def prepare_prompt(self, request: dict):
        """Construit les messages au format OpenAI (compat DeepInfra)."""
        sys = "You are a concise, polite phone assistant. Reply in French (Switzerland)."
        messages = [{"role": "system", "content": sys}]

        tr = request.get("transcript")
        if isinstance(tr, list) and tr:
            for turn in tr:
                role = "assistant" if (turn or {}).get("role") == "assistant" else "user"
                content = (turn or {}).get("content") or ""
                if content:
                    messages.append({"role": role, "content": content})
            return messages

        content = request.get("content")
        if isinstance(content, str) and content.strip():
            messages.append({"role": "user", "content": content.strip()})
            return messages

        delta = request.get("delta")
        if isinstance(delta, str) and delta.strip():
            messages.append({"role": "user", "content": delta.strip()})
            return messages

        messages.append({"role": "user", "content": "Bonjour"})
        return messages

    async def draft_response(self, request: dict):
        """Ton draft_response original, mais relié à self.prepare_prompt et au modèle DeepInfra."""
        prompt = self.prepare_prompt(request)
        sid = request["stream_id"]

        try:
            stream = await self.client.chat.completions.create(
                model=MODEL,
                messages=prompt,
                temperature=0.2,
                stream=True,
            )

            async for chunk in stream:
                delta = chunk.choices[0].delta
                if delta and delta.content:
                    yield {
                        "stream_id": sid,
                        "content": delta.content,
                        "end_of_stream": False,
                    }

            # fin de tour
            yield {
                "stream_id": sid,
                "content": "",
                "end_of_stream": True,
            }

        except APIStatusError as e:
            # Ex: 401/402 (clé invalide / solde insuffisant)
            print(f"[LLM ERROR] {e.status_code}: {getattr(e, 'response', None)}", flush=True)
            yield {
                "stream_id": sid,
                "content": f"Erreur LLM ({e.status_code}). Vérifiez la clé ou le solde.",
                "end_of_stream": True,
            }
        except Exception:
            print(traceback.format_exc(), flush=True)
            yield {
                "stream_id": sid,
                "content": "Désolé, incident technique.",
                "end_of_stream": True,
            }

    async def websocket_handler(self, websocket: WebSocket):
        await websocket.accept()
        current_stream_id = -1

        async def stream_response(request):
            try:
                async for event in self.draft_response(request):
                    # même enveloppe qu’avant, j’ajoute juste flush=True pour TTS
                    await websocket.send_text(json.dumps({
                        "type": "stream_response",
                        "data": { **event, "flush": True }
                    }))
                    if request['stream_id'] < current_stream_id:
                        return  # Got new stream_request, abandon
            except Exception:
                print(traceback.format_exc(), flush=True)

        try:
            while True:
                message = await websocket.receive_text()
                request = json.loads(message)

                if request["type"] == "start_call":
                    current_stream_id = request["data"]['stream_id']
                    first_event = {
                        "type": "stream_response",
                        "data": {
                            "stream_id": current_stream_id,
                            "content": "How can I help you?",  # Agent's first message (garde ta phrase)
                            "flush": True,
                            "end_of_stream": True,
                        }
                    }
                    await websocket.send_text(json.dumps(first_event))

                elif request["type"] == "stream_request":
                    current_stream_id = request["data"]['stream_id']
                    asyncio.create_task(stream_response(request["data"]))

        except WebSocketDisconnect:
            ...

agent = Agent()

# Route FastAPI qui appelle la méthode avec self (agent)
@app.websocket("/llm-websocket")
async def websocket_route(ws: WebSocket):
    await agent.websocket_handler(ws)

# (facultatif) petite route santé
@app.get("/ping")
def ping():
    return {"pong": True}

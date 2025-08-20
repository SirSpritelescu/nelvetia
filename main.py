import os, json, asyncio, traceback
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from openai import AsyncOpenAI, APIStatusError

# --- Config via variables d'env Render ---
BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.deepinfra.com/v1/openai")
API_KEY  = os.getenv("OPENAI_API_KEY", "EAk6LnTYRt2IwArvx9gPSU4Ryxi80wXC")
MODEL    = os.getenv("MODEL_NAME", "openai/gpt-oss-120b")  # ex: openai/gpt-oss-120b

app = FastAPI()

class Agent:
    def __init__(self):
        self.client = AsyncOpenAI(api_key=API_KEY, base_url=BASE_URL)

    def prepare_prompt(self, req_data: dict):
        """
        Millis envoie: stream_request.data.transcript = [<chat_history>]
        On construit messages (OpenAI-format) à partir de ça.
        """
        messages = [{
            "role": "system",
            "content": "You are a concise, polite phone assistant. Reply in French (Switzerland)."
        }]

        tr = req_data.get("transcript")
        if isinstance(tr, list) and tr:
            for turn in tr:
                # Millis envoie typiquement {"role":"user"/"assistant","content":"..."}
                role = "assistant" if (turn or {}).get("role") == "assistant" else "user"
                content = (turn or {}).get("content") or ""
                if content:
                    messages.append({"role": role, "content": content})
        else:
            # Fallback: content/delta direct si jamais
            content = (req_data.get("content") or req_data.get("delta") or "Bonjour").strip()
            messages.append({"role": "user", "content": content})

        return messages

    async def draft_response(self, req_data: dict):
        """
        Répond en STREAMING selon le protocole Millis:
        on envoie plusieurs 'stream_response' avec flush:true,
        puis un 'end_of_stream': true pour terminer le tour. :contentReference[oaicite:1]{index=1}
        """
        sid = req_data["stream_id"]
        messages = self.prepare_prompt(req_data)

        try:
            stream = await self.client.chat.completions.create(
                model=MODEL,
                messages=messages,
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

            # fin du tour
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
                "content": f"Erreur LLM ({e.status_code}). Vérifiez la clé/solde.",
                "end_of_stream": True,
            }
        except Exception:
            print("[LLM CRASH]", traceback.format_exc(), flush=True)
            yield {
                "stream_id": sid,
                "content": "Désolé, incident technique.",
                "end_of_stream": True,
            }

    async def websocket_handler(self, ws: WebSocket):
        await ws.accept()
        current_stream_id = -1

        async def send_stream_response(event: dict):
            # Millis recommande flush pour déclencher la TTS immédiatement. :contentReference[oaicite:2]{index=2}
            await ws.send_text(json.dumps({
                "type": "stream_response",
                "data": { **event, "flush": True }
            }))

        async def handle_stream_request(req_data: dict):
            try:
                async for event in self.draft_response(req_data):
                    await send_stream_response(event)
                    if req_data["stream_id"] < current_stream_id:
                        return  # un nouveau tour a pris la main
            except Exception:
                print(traceback.format_exc(), flush=True)
                await send_stream_response({
                    "stream_id": req_data["stream_id"],
                    "content": "Désolé, incident technique.",
                    "end_of_stream": True
                })

        try:
            while True:
                raw = await ws.receive_text()
                msg = json.loads(raw)
                mtype = msg.get("type")
                data = msg.get("data", {}) or {}

                if mtype == "start_call":
                    # Premier message: utiliser stream_id du start_call pour la 1ère réponse. :contentReference[oaicite:3]{index=3}
                    current_stream_id = data["stream_id"]
                    await send_stream_response({
                        "stream_id": current_stream_id,
                        "content": "How can I help you?",
                        "end_of_stream": True
                    })

                elif mtype == "stream_request":
                    current_stream_id = data["stream_id"]
                    asyncio.create_task(handle_stream_request(data))

                # Événements optionnels documentés par Millis (on log pour debug) :contentReference[oaicite:4]{index=4}
                elif mtype == "partial_transcript":
                    # data: {session_id, transcript, is_final}
                    print("[partial_transcript]", data, flush=True)
                elif mtype == "playback_finished":
                    # data: {session_id, stream_id}
                    print("[playback_finished]", data, flush=True)
                elif mtype == "interrupt":
                    # {type:"interrupt", "stream_id": int}
                    print("[interrupt]", data, flush=True)
                else:
                    # Inconnu → ignorer poliment
                    print("[unknown event]", msg, flush=True)

        except WebSocketDisconnect:
            print("[WS] disconnected", flush=True)

agent = Agent()

@app.websocket("/llm-websocket")
async def ws_route(ws: WebSocket):
    await agent.websocket_handler(ws)

@app.get("/ping")
def ping():
    return {"pong": True}

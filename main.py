import os, asyncio, json, traceback
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from openai import AsyncOpenAI, APIStatusError

# ---------- Config DeepInfra via variables d'env (Render > Environment) ----------
BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.deepinfra.com/v1/openai")
API_KEY  = os.getenv("OPENAI_API_KEY", "EAk6LnTYRt2IwArvx9gPSU4Ryxi80wXC")
MODEL    = os.getenv("MODEL_NAME", "openai/gpt-oss-120b")  # ex: openai/gpt-oss-120b

app = FastAPI()
client = AsyncOpenAI(api_key=API_KEY, base_url=BASE_URL)

@app.websocket("/llm-websocket")
async def websocket_handler(websocket: WebSocket):
    await websocket.accept()
    current_stream_id = -1

    async def stream_response(request):
        try:
            async for event in draft_response(request):
                # Millis préfère recevoir flush: true pour déclencher la TTS immédiatement
                await websocket.send_text(json.dumps({
                    "type": "stream_response",
                    "data": { **event, "flush": True }
                }))
                # Abandonner si un nouveau stream_id a pris la main
                if request['stream_id'] < current_stream_id:
                    return
        except Exception:
            print(traceback.format_exc(), flush=True)
            await websocket.send_text(json.dumps({
                "type": "stream_response",
                "data": {
                    "stream_id": request.get('stream_id', 0),
                    "content": "Désolé, incident technique.",
                    "flush": True,
                    "end_of_stream": True
                }
            }))

    try:
        while True:
            message = await websocket.receive_text()
            request = json.loads(message)
            # print("[WS IN]", request, flush=True)  # décommente pour debug

            if request["type"] == "start_call":
                current_stream_id = request["data"]["stream_id"]
                await websocket.send_text(json.dumps({
                    "type": "stream_response",
                    "data": {
                        "stream_id": current_stream_id,
                        "content": "Bonjour, je vous écoute.",
                        "flush": True,
                        "end_of_stream": True
                    }
                }))

            elif request["type"] == "stream_request":
                current_stream_id = request["data"]["stream_id"]
                asyncio.create_task(stream_response(request["data"]))

    except WebSocketDisconnect:
        # print("Client disconnected", flush=True)
        ...

def _extract_transcript(req_data: dict) -> str:
    """
    Millis peut envoyer:
      - req_data["transcript"] = "texte..."
      - req_data["transcript"] = [{"role": "...", "content": "..."}]
    On retourne un texte utilisateur raisonnable.
    """
    tr = req_data.get("transcript")

    if isinstance(tr, str):
        return tr.strip()

    if isinstance(tr, list) and tr:
        # On prend le dernier message non-vide (souvent celui de l'utilisateur)
        for turn in reversed(tr):
            if isinstance(turn, dict):
                txt = (turn.get("content") or "").strip()
                if txt:
                    return txt
    return "Bonjour"

async def draft_response(request: dict):
    """
    Appel DeepInfra (OpenAI-compatible) en streaming.
    Rend des events {stream_id, content, end_of_stream}.
    """
    sid = request["stream_id"]
    user_text = _extract_transcript(request)

    messages = [
        {"role": "system", "content": "Tu es un assistant téléphonique, bref et poli. Réponds en français (Suisse)."},
        {"role": "user", "content": user_text}
    ]

    try:
        stream = await client.chat.completions.create(
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
                    "end_of_stream": False
                }

        # Fin du tour
        yield {
            "stream_id": sid,
            "content": "",
            "end_of_stream": True
        }

    except APIStatusError as e:
        # ex: 401/402 si clé invalide/solde à 0
        print(f"[LLM ERROR] status={e.status_code} resp={getattr(e, 'response', None)}", flush=True)
        yield {
            "stream_id": sid,
            "content": f"Erreur LLM ({e.status_code}). Vérifiez la clé ou le solde.",
            "end_of_stream": True
        }
    except Exception:
        print(traceback.format_exc(), flush=True)
        yield {
            "stream_id": sid,
            "content": "Désolé, incident technique.",
            "end_of_stream": True
        }

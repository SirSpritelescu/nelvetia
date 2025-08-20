import os, json, asyncio, traceback
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from openai import AsyncOpenAI, APIStatusError

# === Config via variables d'env (Render > Environment) ===
BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.deepinfra.com/v1/openai")
API_KEY  = os.getenv("OPENAI_API_KEY", "EAk6LnTYRt2IwArvx9gPSU4Ryxi80wXC")
MODEL    = os.getenv("MODEL_NAME", "openai/gpt-oss-120b")  # ou meta-llama/...

app = FastAPI()
client = AsyncOpenAI(api_key=API_KEY, base_url=BASE_URL)

@app.get("/")
def root():
    return JSONResponse({"status": "ok"})

@app.get("/ping")
def ping():
    return {"pong": True}

@app.websocket("/llm-websocket")
async def websocket_handler(websocket: WebSocket):
    await websocket.accept()

    # On garde la logique "abandon si nouveau stream_id arrive"
    current_stream_id = -1

    async def stream_response(request):
        nonlocal current_stream_id
        sid = request.get("stream_id", 0) or 0
        try:
            async for event in draft_response(request):
                # flush: True pour forcer la lecture TTS côté Millis
                payload = {
                    "type": "stream_response",
                    "data": {
                        **event,
                        "flush": True,
                    }
                }
                await websocket.send_text(json.dumps(payload))

                # si un nouveau stream_request a pris la main, on abandonne
                if sid < current_stream_id:
                    return
        except Exception:
            print(traceback.format_exc(), flush=True)
            # message d'erreur lisible (pas de silence)
            await websocket.send_text(json.dumps({
                "type": "stream_response",
                "data": {
                    "stream_id": sid,
                    "content": "Désolé, incident technique.",
                    "flush": True,
                    "end_of_stream": True
                }
            }))

    try:
        # Accueil immédiat même si Millis n’envoie pas start_call
        await websocket.send_text(json.dumps({
            "type": "stream_response",
            "data": {
                "stream_id": 1,
                "content": "Bonjour, je vous écoute.",
                "flush": True,
                "end_of_stream": True
            }
        }))

        while True:
            message = await websocket.receive_text()
            request = json.loads(message)

            if request.get("type") == "start_call":
                current_stream_id = request["data"]["stream_id"]
                first_event = {
                    "type": "stream_response",
                    "data": {
                        "stream_id": current_stream_id,
                        "content": "Comment puis-je vous aider ?",
                        "flush": True,
                        "end_of_stream": True,
                    }
                }
                await websocket.send_text(json.dumps(first_event))

            elif request.get("type") == "stream_request":
                current_stream_id = request["data"]["stream_id"]
                # traitement async pour autoriser plusieurs tours rapides
                asyncio.create_task(stream_response(request["data"]))

            else:
                # on ignore poliment les autres types
                pass

    except WebSocketDisconnect:
        ...

def prepare_prompt(request):
    """
    Construit les messages OpenAI à partir de ce que Millis envoie.
    Gère data.transcript[] ou data.content / data.delta.
    """
    sys = ("Tu es une assistante téléphonique. "
           "Parle français (Suisse). Une seule question à la fois. "
           "Réponses brèves et polies.")
    messages = [{"role": "system", "content": sys}]

    # 1) transcript (préféré)
    transcript = request.get("transcript")
    if isinstance(transcript, list) and transcript:
        for turn in transcript:
            role = "assistant" if (turn or {}).get("role") == "assistant" else "user"
            content = (turn or {}).get("content") or ""
            if content:
                messages.append({"role": role, "content": content})
        return messages

    # 2) content direct
    content = request.get("content")
    if isinstance(content, str) and content.strip():
        messages.append({"role": "user", "content": content.strip()})
        return messages

    # 3) delta (partial)
    delta = request.get("delta")
    if isinstance(delta, str) and delta.strip():
        messages.append({"role": "user", "content": delta.strip()})
        return messages

    # fallback minimal pour éviter le vide
    messages.append({"role": "user", "content": "Bonjour"})
    return messages

async def draft_response(request):
    """
    Appelle DeepInfra (API OpenAI-compatible) en streaming.
    Rend des events {stream_id, content, end_of_stream}.
    """
    sid = request.get("stream_id", 0) or 0
    messages = prepare_prompt(request)

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
                    "end_of_stream": False,
                }

        # fin de tour
        yield {
            "stream_id": sid,
            "content": "",
            "end_of_stream": True,
        }

    except APIStatusError as e:
        # Ex: 402 balance insuffisante → message explicite
        print(f"[LLM ERROR] status={e.status_code} body={getattr(e, 'response', None)}", flush=True)
        yield {
            "stream_id": sid,
            "content": f"Erreur LLM ({e.status_code}). Vérifiez le solde ou la clé.",
            "end_of_stream": True,
        }

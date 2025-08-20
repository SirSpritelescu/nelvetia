import json, asyncio, traceback
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

app = FastAPI()

@app.get("/ping")
def ping():
    return {"pong": True}

@app.websocket("/llm-websocket")
async def websocket_handler(ws: WebSocket):
    await ws.accept()
    current_stream_id = -1

    async def send_text(stream_id: int, text: str, final: bool):
        # Millis lit mieux quand on envoie flush:true
        await ws.send_text(json.dumps({
            "type": "stream_response",
            "data": {
                "stream_id": stream_id,
                "content": text,
                "flush": True,
                "end_of_stream": final
            }
        }))

    def extract_user_text(data: dict) -> str:
        """
        Gère plusieurs schémas possibles:
        - data["content"] = "...".
        - data["transcript"] = [{"role":"user","content":"..."}]
        - data["delta"] = "..." (partial)
        """
        # 1) content direct
        c = data.get("content")
        if isinstance(c, str) and c.strip():
            return c.strip()

        # 2) transcript tableau
        trans = data.get("transcript") or []
        if isinstance(trans, list) and trans:
            # prend le dernier message non vide
            for turn in reversed(trans):
                txt = (turn or {}).get("content", "")
                if isinstance(txt, str) and txt.strip():
                    return txt.strip()

        # 3) delta (partial)
        d = data.get("delta")
        if isinstance(d, str) and d.strip():
            return d.strip()

        return ""

    async def handle_stream_request(req_data: dict):
        try:
            user_msg = extract_user_text(req_data)
            sid = req_data.get("stream_id", 0) or 0

            if not user_msg:
                # Rien d’exploitable → on ignore gentiment (pas de TTS vide)
                return

            # Envoie en 2 temps pour forcer la lecture et montrer que ça vit
            await send_text(sid, "D’accord.", False)
            await send_text(sid, f"Tu as dit: {user_msg}", True)

        except Exception:
            print("[WS ERROR]", traceback.format_exc(), flush=True)
            sid = req_data.get("stream_id", 0) or 0
            await send_text(sid, "Désolé, incident technique.", True)

    try:
        # Message d’accueil immédiat pour éviter les silences si Millis ne fait pas start_call
        await send_text(1, "Bonjour, je vous écoute.", True)

        while True:
            raw = await ws.receive_text()
            req = json.loads(raw)

            rtype = req.get("type")
            data = req.get("data", {})
            print(f"[WS IN] type={rtype} keys={list(data.keys())}", flush=True)

            if rtype == "start_call":
                current_stream_id = data.get("stream_id", 1) or 1
                await send_text(current_stream_id, "Bonjour, je vous écoute.", True)

            elif rtype == "stream_request":
                current_stream_id = data.get("stream_id", current_stream_id or 2)
                # traitement async pour ne pas bloquer
                asyncio.create_task(handle_stream_request(data))

            else:
                # Inconnu: on log et on ignore
                print(f"[WS WARN] unknown type: {rtype}", flush=True)

    except WebSocketDisconnect:
        print("[WS] client disconnected", flush=True)

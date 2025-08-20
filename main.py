import json, asyncio, traceback
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

app = FastAPI()

@app.websocket("/llm-websocket")
async def websocket_handler(ws: WebSocket):
    await ws.accept()
    current_stream_id = -1

    async def stream_response(request: dict):
        try:
            user_msg = request["transcript"][-1]["content"]
            reply = f"Tu as dit: {user_msg}"

            await ws.send_text(json.dumps({
                "type": "stream_response",
                "data": {
                    "stream_id": request["stream_id"],
                    "content": reply,
                    "end_of_stream": True
                }
            }))
        except Exception:
            print(traceback.format_exc(), flush=True)
            await ws.send_text(json.dumps({
                "type": "stream_response",
                "data": {
                    "stream_id": request["stream_id"],
                    "content": "Erreur technique.",
                    "end_of_stream": True
                }
            }))

    try:
        while True:
            message = await ws.receive_text()
            request = json.loads(message)

            if request["type"] == "start_call":
                current_stream_id = request["data"]["stream_id"]
                await ws.send_text(json.dumps({
                    "type": "stream_response",
                    "data": {
                        "stream_id": current_stream_id,
                        "content": "Bonjour, je vous écoute.",
                        "end_of_stream": True
                    }
                }))

            elif request["type"] == "stream_request":
                current_stream_id = request["data"]["stream_id"]
                asyncio.create_task(stream_response(request["data"]))

    except WebSocketDisconnect:
        print("Client disconnected")

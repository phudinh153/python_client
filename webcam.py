import argparse
import asyncio
from asyncio import Queue
from http.client import USE_PROXY
import json
import logging
import os
import platform
import re
import ssl
import signal
from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCIceServer, RTCConfiguration
from aiortc.contrib.media import MediaPlayer, MediaRelay
from aiortc.mediastreams import MediaStreamTrack
from aiortc.rtcrtpsender import RTCRtpSender
from requests import options
import socketio

ROOT = os.path.dirname(__file__)
offer_queue = Queue()
sio = socketio.AsyncClient()
relay = None
webcam = None
USERNAME = "webcam"
ROOM = ["1", "2", "3", "4", "5"]
pcs = {}


async def join_room(room) -> None:
    print("emit join")
    print(f"username: {USERNAME}, room: {room}")
    await sio.emit("join", {"username": USERNAME, "room": room})


async def start_server() -> None:
    # Connect to the signaling server
    # signaling_server = "http://127.0.0.1:5004"
    signaling_server = "https://signaling-server-pfm2.onrender.com/"

    # @sio.event
    # async def connect() -> None:
    #     print("Connected to the signaling server")

    # Disconnect from the signaling server
    # @sio.event
    # async def disconnect() -> None:
    #     print("Disconnected from the signaling server")
    @sio.event
    async def offer(data) -> None:
        # Add the offer to the queue
        await offer_queue.put(data)

    async def process_offers():
        while True:
            # Wait for an offer to be added to the queue
            data = await offer_queue.get()
            params = data
            offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])
            room = data["room"]
            if room in pcs:
                print("Room already in use")
                raise Exception("Room already in use")

            ice_servers = [
                RTCIceServer(
                    urls=["stun:stun1.l.google.com:19302", "stun:stun2.l.google.com:19302"]
                ),
            ]
            config = RTCConfiguration(iceServers=ice_servers)
            pc = RTCPeerConnection(config)
            # pcs.add(pc)
            pcs[room] = pc

            @pc.on("connectionstatechange")
            async def on_connectionstatechange():
                print("Connection state is %s" % pc.connectionState)
                if pc.connectionState == "failed":
                    await pc.close()
                    del pcs[room]

            # open media source
            audio, video = create_local_tracks(
                args.play_from, decode=not args.play_without_decoding
            )

            if audio:
                audio_sender = pc.addTrack(audio)
                if args.audio_codec:
                    force_codec(pc, audio_sender, args.audio_codec)
                elif args.play_without_decoding:
                    raise Exception("You must specify the audio codec using --audio-codec")

            if video:
                video_sender = pc.addTrack(video)
                if args.video_codec:
                    force_codec(pc, video_sender, args.video_codec)
                elif args.play_without_decoding:
                    raise Exception("You must specify the video codec using --video-codec")

            await pc.setRemoteDescription(offer)

            answer = await pc.createAnswer()
            await pc.setLocalDescription(answer)

            await sio.emit(
                "answer",
                {
                    "sdp": pc.localDescription.sdp,
                    "type": pc.localDescription.type,
                    "username": USERNAME,
                    "room": room,
                },
            )
            print("emit answer")
            # return web.Response(
            #     content_type="application/json",
            #     text=json.dumps(
            #         {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}
            #     ),
            # )
            # Indicate that the offer has been processed
            offer_queue.task_done()

    @sio.event
    async def connect() -> None:
        try:
            print("Connected to server %s" % signaling_server)
            # await send_ack()
            # await join_room()
            for room in ROOM:
                await join_room(room)

        except Exception as e:
            print("Error in connect event handler: ", e)

    asyncio.create_task(process_offers())
    try:
        await sio.connect(signaling_server)
        await sio.wait()
    except Exception as e:
        print("Exception occurred: ", e)
        os.kill(os.getpid(), signal.SIGILL)


def create_local_tracks(
    play_from, decode
) -> tuple[MediaStreamTrack, MediaStreamTrack] | tuple[None, MediaStreamTrack]:
    global relay, webcam
    
    if play_from:
        player = MediaPlayer(play_from, decode=decode)
        print(player)
        return player.audio, player.video
    else:
        # options = {"framerate": "30", "video_size": "640x480"}
        # options = {"framerate": "15", "video_size": "640x480"}
        options = {"framerate": "10", "video_size": "160x120"}
        if relay is None:
            if platform.system() == "Darwin":
                webcam = MediaPlayer(
                    "default:none", format="avfoundation", options=options
                )
            elif platform.system() == "Windows":
                webcam = MediaPlayer(
                    "video=Integrated Camera",
                    format="dshow",
                    options=options,
                    # "video=Integrated Camera", format="dshow",
                )
            else:
                webcam = MediaPlayer("/dev/video0", format="v4l2", options=options)
            relay = MediaRelay()
        return None, relay.subscribe(webcam.video)


def force_codec(pc, sender, forced_codec) -> None:
    kind = forced_codec.split("/")[0]
    codecs = RTCRtpSender.getCapabilities(kind).codecs
    transceiver = next(t for t in pc.getTransceivers() if t.sender == sender)
    transceiver.setCodecPreferences(
        [codec for codec in codecs if codec.mimeType == forced_codec]
    )


async def index(request):
    content = open(os.path.join(ROOT, "index.html"), "r").read()
    return web.Response(content_type="text/html", text=content)


async def javascript(request):
    content = open(os.path.join(ROOT, "client.js"), "r").read()
    return web.Response(content_type="application/javascript", text=content)


async def on_shutdown(app):
    # close peer connections
    coros = [pc.close() for pc in pcs]
    await asyncio.gather(*coros)
    pcs.clear()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="WebRTC webcam demo")
    parser.add_argument("--cert-file", help="SSL certificate file (for HTTPS)")
    parser.add_argument("--key-file", help="SSL key file (for HTTPS)")
    parser.add_argument("--play-from", help="Read the media from a file and sent it.")
    parser.add_argument(
        "--play-without-decoding",
        help=(
            "Read the media without decoding it (experimental). "
            "For now it only works with an MPEGTS container with only H.264 video."
        ),
        action="store_true",
    )
    parser.add_argument(
        "--host", default="0.0.0.0", help="Host for HTTP server (default: 0.0.0.0)"
    )
    parser.add_argument(
        "--port", type=int, default=8080, help="Port for HTTP server (default: 8080)"
    )
    parser.add_argument("--verbose", "-v", action="count")
    parser.add_argument(
        "--audio-codec", help="Force a specific audio codec (e.g. audio/opus)"
    )
    parser.add_argument(
        "--video-codec", help="Force a specific video codec (e.g. video/H264)"
    )

    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    if args.cert_file:
        ssl_context = ssl.SSLContext()
        ssl_context.load_cert_chain(args.cert_file, args.key_file)
    else:
        ssl_context = None

    # app = web.Application()
    # app.on_shutdown.append(on_shutdown)
    # app.router.add_get("/", index)
    # app.router.add_get("/client.js", javascript)
    # app.router.add_post("/offer", offer)
    loop = asyncio.get_event_loop()
    loop.run_until_complete(start_server())
    # web.run_app(app, host=args.host, port=args.port, ssl_context=ssl_context)

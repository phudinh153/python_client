import argparse
import asyncio
from http.client import USE_PROXY
import json
import logging
from msilib.schema import Media
import os
import platform
import ssl
import signal
from aiohttp import web
from aiortc import (
    RTCPeerConnection,
    RTCSessionDescription,
    RTCIceServer,
    RTCConfiguration,
)
from aiortc.contrib.media import MediaPlayer, MediaRelay
from aiortc.mediastreams import MediaStreamTrack
from aiortc.rtcrtpsender import RTCRtpSender
from requests import options
import socketio

from video_track import VideoTransformTrack

ROOT = os.path.dirname(__file__)

sio = socketio.AsyncClient()
relay = MediaRelay()
webcam = None
USERNAME = "webcam"
ROOM = "1"


async def join_room() -> None:
    print("emit join")
    print(f"username: {USERNAME}, room: {ROOM}")
    await sio.emit("join", {"username": USERNAME, "room": ROOM})


async def start_server(args) -> None:
    # Connect to the signaling server
    # signaling_server = "http://127.0.0.1:5004"
    signaling_server = "https://signaling-server-pfm2.onrender.com/"

    @sio.event
    async def offer(data) -> None:
        params = data
        offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])
        ice_servers = [
            RTCIceServer(
                urls=["stun:stun1.l.google.com:19302", "stun:stun2.l.google.com:19302"]
            ),
        ]
        config = RTCConfiguration(iceServers=ice_servers)
        pc = RTCPeerConnection(config)
        pcs.add(pc)
        player = MediaPlayer(os.path.join(ROOT, "test.mkv"))

        @pc.on("iceconnectionstatechange")
        async def on_iceconnectionstatechange():
            print("ICE connection state is %s" % pc.iceConnectionState)

        @pc.on("connectionstatechange")
        async def on_connectionstatechange():
            print("Connection state is %s" % pc.connectionState)
            if pc.connectionState == "failed":
                await pc.close()
                pcs.discard(pc)

        # open media source
        # audio, video = create_local_tracks(
        #     args.play_from, decode=not args.play_without_decoding
        # )
        # @pc.on("track")
        # def on_track(track):
        #     # log_info("Track %s received", track.kind)
        #     print("Track %s received" % track.kind)

        #     if track.kind == "audio":
        #         pc.addTrack(player.audio)
        #     elif track.kind == "video":
        #         pc.addTrack(
        #             VideoTransformTrack(
        #                 relay.subscribe(track), transform=params["video_transform"]
        #             )
        #         )
        pc.addTrack(
            VideoTransformTrack(player.video, transform=params.get("video_transform", None))
        )

        # handle offer
        await pc.setRemoteDescription(offer)

        # send answer
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)

        await sio.emit(
            "answer",
            {
                "sdp": pc.localDescription.sdp,
                "type": pc.localDescription.type,
                "username": USERNAME,
                "room": ROOM,
            },
        )
        print("emit answer")

    @sio.event
    async def connect() -> None:
        try:
            print("Connected to server %s" % signaling_server)
            # await send_ack()
            await join_room()
        except Exception as e:
            print("Error in connect event handler: ", e)

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


pcs = set()


async def on_shutdown(app):
    # close peer conn ections
    coros = [pc.close() for pc in pcs]
    await asyncio.gather(*coros)
    pcs.clear()


async def main():
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

    # loop = asyncio.get_event_loop()
    # loop.run_until_complete(start_server(args))
    await start_server(args)


if __name__ == "__main__":
    asyncio.run(main())

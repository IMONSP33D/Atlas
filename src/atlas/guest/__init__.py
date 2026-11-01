"""Guest-agent wire protocol, shared by the host (ProxmoxBackend) and the in-VM agent."""
from .protocol import (
    MAX_FRAME, PROTOCOL_MAJOR, Frame, FrameError, FrameReader, FrameTooLarge,
    decode_frame, encode_frame, read_frame, write_frame,
)

__all__ = [
    "MAX_FRAME", "PROTOCOL_MAJOR", "Frame", "FrameError", "FrameReader", "FrameTooLarge",
    "decode_frame", "encode_frame", "read_frame", "write_frame",
]

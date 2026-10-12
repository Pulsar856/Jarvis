"""Structured ALSA hardware discovery and explicit device selection."""

import re
from dataclasses import dataclass
from typing import Literal

from jarvis.audio.errors import DeviceNotFound

Direction = Literal["capture", "playback"]
DEVICE_LINE = re.compile(
    r"^card (\d+): ([A-Za-z0-9_-]+) \[([^\]]+)\], "
    r"device (\d+): ([^\[]+)\[",
    re.MULTILINE,
)


@dataclass(frozen=True)
class AudioDevice:
    """One detected hardware interface; enumeration is not an access test."""

    direction: Direction
    card_index: int
    card_id: str
    card_name: str
    device_index: int
    device_name: str
    native_capabilities: tuple[str, ...] = ()
    persistent_id: str | None = None

    @property
    def identifier(self) -> str:
        """Prefer a USB by-id identity; fall back to the named ALSA card."""
        if self.persistent_id is not None:
            return f"{self.persistent_id}:{self.device_index}"
        return self.alsa_identifier

    @property
    def alsa_identifier(self) -> str:
        return f"{self.card_id}:{self.device_index}"

    @property
    def pcm(self) -> str:
        """Negotiate PCM format/channels through the current named endpoint."""
        return f"plughw:CARD={self.card_id},DEV={self.device_index}"

    @property
    def is_jabra(self) -> bool:
        return "jabra" in (self.card_name + " " + self.device_name).casefold()


def parse_devices(text: str, direction: Direction) -> tuple[AudioDevice, ...]:
    """Parse the C-locale hardware listing emitted by arecord/aplay -l."""
    return tuple(
        AudioDevice(
            direction=direction,
            card_index=int(card),
            card_id=card_id,
            card_name=name,
            device_index=int(device),
            device_name=device_name.strip(),
        )
        for card, card_id, name, device, device_name
        in DEVICE_LINE.findall(text)
    )


def native_capabilities(text: str, direction: Direction) -> tuple[str, ...]:
    """Keep USB driver metadata descriptive, including any rate ranges."""
    sections = re.split(r"^(Playback|Capture):\s*$", text, flags=re.MULTILINE)
    label = direction.capitalize()
    for index in range(1, len(sections), 2):
        if sections[index] == label:
            return tuple(
                line.strip() for line in sections[index + 1].splitlines()
                if line.strip().startswith(
                    ("Format:", "Channels:", "Rates:", "Bits:")
                )
            )
    return ()


def select_device(
    devices: tuple[AudioDevice, ...], selector: str, direction: Direction
) -> AudioDevice:
    """Resolve 'jabra' or an exact discovered ID; refuse ambiguous matches."""
    selector = selector.strip()
    matches = tuple(
        device for device in devices
        if device.direction == direction and (
            device.is_jabra if selector.casefold() == "jabra"
            else selector in (device.identifier, device.alsa_identifier)
        )
    )
    if not matches:
        raise DeviceNotFound(
            f"No {direction} device matches {selector!r}; "
            "inspect 'jarvis audio devices'."
        )
    if len(matches) > 1:
        raise DeviceNotFound(
            f"Multiple {direction} devices match {selector!r}; "
            "select an exact listed ID."
        )
    return matches[0]

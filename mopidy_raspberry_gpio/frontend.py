import logging
import threading
import time

import pykka
from mopidy import core, models

from .rotencoder import RotEncoder

logger = logging.getLogger(__name__)


class RaspberryGPIOFrontend(pykka.ThreadingActor, core.CoreListener):
    def __init__(self, config, core: core.Core):
        super().__init__()
        import RPi.GPIO as GPIO

        self.core = core
        self.config = config["raspberry-gpio"]
        self.pin_settings = {}
        self.rot_encoders = {}

        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BCM)

        self.last_states = {}
        self._stop_polling = threading.Event()

        # Setup pins
        for key in self.config:
            if key.startswith("bcm"):
                pin = int(key.replace("bcm", ""))
                settings = self.config[key]
                if settings is None:
                    continue

                logger.info("Configuring " + key + " " + str(settings))
                pull = GPIO.PUD_UP if settings.active != "active_high" else GPIO.PUD_DOWN

                # Handle rotary encoders
                if "rotenc_id" in settings.options:
                    rotenc_id = settings.options["rotenc_id"]
                    encoder = self.rot_encoders.get(rotenc_id)
                    if not encoder:
                        encoder = RotEncoder(rotenc_id)
                        self.rot_encoders[rotenc_id] = encoder
                    encoder.add_pin(pin, settings.event)

                GPIO.setup(pin, GPIO.IN, pull_up_down=pull)
                self.pin_settings[pin] = settings
                self.last_states[pin] = GPIO.input(pin)

    def on_start(self):
        # Start polling thread
        self._polling_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._polling_thread.start()

    def on_stop(self):
        self._stop_polling.set()
        self._polling_thread.join(timeout=1)
        import RPi.GPIO as GPIO
        GPIO.cleanup()

    def _poll_loop(self):
        import RPi.GPIO as GPIO

        while not self._stop_polling.is_set():
            for pin, settings in self.pin_settings.items():
                state = GPIO.input(pin)
                last = self.last_states[pin]

                # Rotary encoder pins: fast polling, no debounce
                if "rotenc_id" in settings.options:
                    if state != last:
                        encoder = self.rot_encoders[settings.options["rotenc_id"]]
                        event = encoder.get_event()
                        if event:
                            logger.info(
                                "Rotary encoder bcm%d event: %s", pin, event
                            )
                            self.dispatch_input(event, settings.options)
                    self.last_states[pin] = state
                    continue

                # Button pins: detect edges with debounce
                active_low = settings.active != "active_high"
                if (active_low and last == GPIO.HIGH and state == GPIO.LOW) or (
                    not active_low and last == GPIO.LOW and state == GPIO.HIGH
                ):
                    self.gpio_event(pin)
                    time.sleep(settings.bouncetime / 1000.0)  # debounce

                self.last_states[pin] = state

            # Small sleep to reduce CPU usage, but fast enough for encoders
            time.sleep(0.0005)  # 2 kHz polling

    def find_pin_rotenc(self, pin):
        for encoder in self.rot_encoders.values():
            if pin in encoder.pins:
                return encoder

    def gpio_event(self, pin):
        settings = self.pin_settings[pin]
        event = settings.event
        encoder = self.find_pin_rotenc(pin)
        if encoder:
            event = encoder.get_event()

        if event:
            logger.info(
                "GPIO bcm%d event: %s (%s)", pin, event, str(settings.options)
            )
            self.dispatch_input(event, settings.options)

    def dispatch_input(self, event, options):
        handler_name = f"handle_{event}"
        try:
            getattr(self, handler_name)(options)
        except AttributeError:
            raise RuntimeError(f"Could not find input handler for event: {event}")

    def handle_play_pause(self, config):
        if self.core.playback.get_state().get() == core.PlaybackState.PLAYING:
            self.core.playback.pause()
        else:
            self.core.playback.play()

    def handle_play_stop(self, config):
        if self.core.playback.get_state().get() == core.PlaybackState.PLAYING:
            self.core.playback.stop()
        else:
            self.core.playback.play()

    def handle_next(self, config):
        self.core.playback.next()

    def handle_prev(self, config):
        self.core.playback.previous()

    def handle_volume_up(self, config):
        step = int(config.get("step", 5))
        volume = self.core.mixer.get_volume().get()
        volume = min(volume + step, 100)
        self.core.mixer.set_volume(volume)

    def handle_volume_down(self, config):
        step = int(config.get("step", 5))
        volume = self.core.mixer.get_volume().get()
        volume = max(volume - step, 0)
        self.core.mixer.set_volume(volume)

    def handle_playlist(self, config):
        playlist: models.Playlist = self.core.playlists.lookup(config.get("uri")).get()
        self.core.tracklist.clear()
        self.core.tracklist.add(tracks=playlist.tracks)
        self.core.playback.play()

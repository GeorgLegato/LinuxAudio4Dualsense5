# ds5_membrane_sink — nativer PipeWire-Sink fuer den DualSense-Membran-Speaker
#
# Voraussetzungen:
#   sudo apt install libpipewire-0.3-dev libopus-dev zlib1g-dev gcc make

CC      ?= gcc
CFLAGS  ?= -O2 -Wall -Wextra
PKGS     = libpipewire-0.3 opus zlib
CFLAGS  += $(shell pkg-config --cflags $(PKGS)) -pthread
LDLIBS   = $(shell pkg-config --libs $(PKGS)) -lm -pthread

TARGET   = ds5_membrane_sink

VERSION ?= $(shell git describe --tags --always 2>/dev/null || echo dev)
ARCH    := $(shell uname -m)
RELNAME  = ds5-bt-audio-$(VERSION)-$(ARCH)
DISTDIR  = ../dist
STAGE    = $(DISTDIR)/$(RELNAME)

$(TARGET): ds5_membrane_sink.c
	$(CC) $(CFLAGS) -o $@ $< $(LDLIBS)

clean:
	rm -f $(TARGET)
	rm -rf $(DISTDIR)

# Prebuilt release tarball: binary + service + install.sh, no compiler needed on
# the target (works on immutable distros like SteamOS — installs into ~/.local).
release: $(TARGET)
	rm -rf $(STAGE)
	mkdir -p $(STAGE)/usb
	cp $(TARGET) ds5-membrane-sink.service $(STAGE)/
	cp ../packaging/install.sh ../packaging/uninstall.sh $(STAGE)/
	-cp ../usb/* $(STAGE)/usb/ 2>/dev/null
	cp ../README.md $(STAGE)/
	chmod +x $(STAGE)/install.sh $(STAGE)/uninstall.sh
	tar -C $(DISTDIR) -czf $(DISTDIR)/$(RELNAME).tar.gz $(RELNAME)
	@echo ""
	@echo "Built $(DISTDIR)/$(RELNAME).tar.gz"
	@echo "Target install:  tar xzf $(RELNAME).tar.gz && cd $(RELNAME) && ./install.sh"

# In den eigenen ~/.local/bin installieren (kein root noetig)
install: $(TARGET)
	install -Dm755 $(TARGET) $(HOME)/.local/bin/$(TARGET)
	@echo "Installiert nach ~/.local/bin/$(TARGET)"

# Als systemd-User-Service einrichten: startet beim Login, wartet auf den
# Controller, erscheint automatisch als "DualSense BT Speaker".
SERVICE_DIR = $(HOME)/.config/systemd/user
service: install
	install -Dm644 ds5-membrane-sink.service $(SERVICE_DIR)/ds5-membrane-sink.service
	systemctl --user daemon-reload
	systemctl --user enable --now ds5-membrane-sink.service
	@echo ""
	@echo "Service laeuft. Status:  systemctl --user status ds5-membrane-sink"
	@echo "Stoppen/aus:             systemctl --user disable --now ds5-membrane-sink"

service-uninstall:
	-systemctl --user disable --now ds5-membrane-sink.service
	rm -f $(SERVICE_DIR)/ds5-membrane-sink.service
	systemctl --user daemon-reload
	@echo "Service entfernt."

# --- USB variant -------------------------------------------------------------
# For wired use (or if you have no Bluetooth). Needs pydualsense:
#   pip install --user pydualsense
USB_DIR   = ../usb
USB_SHARE = $(HOME)/.local/share/ds5-usb-speaker
service-usb:
	install -Dm755 $(USB_DIR)/ds5-usb-speaker.sh $(USB_SHARE)/ds5-usb-speaker.sh
	install -Dm755 $(USB_DIR)/ds5-usb-setup.sh   $(USB_SHARE)/ds5-usb-setup.sh
	install -Dm755 $(USB_DIR)/ds5_usb_enable.py  $(USB_SHARE)/ds5_usb_enable.py
	install -Dm644 $(USB_DIR)/ds5-usb-speaker.service $(SERVICE_DIR)/ds5-usb-speaker.service
	systemctl --user daemon-reload
	systemctl --user enable --now ds5-usb-speaker.service
	@echo ""
	@echo "USB service running. Status:  systemctl --user status ds5-usb-speaker"

service-usb-uninstall:
	-systemctl --user disable --now ds5-usb-speaker.service
	rm -f $(SERVICE_DIR)/ds5-usb-speaker.service
	rm -rf $(USB_SHARE)
	systemctl --user daemon-reload
	@echo "USB service removed."

# --- Handheld mode: stream the game to a phone (Sunshine host for Moonlight) --
# Video -> phone, sound/input/rumble/LEDs -> DualSense. See ../handheld/README.md
moonlight:
	../handheld/setup-moonlight.sh

.PHONY: clean install release moonlight service service-uninstall service-usb service-usb-uninstall

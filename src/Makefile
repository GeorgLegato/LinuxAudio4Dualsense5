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

$(TARGET): ds5_membrane_sink.c
	$(CC) $(CFLAGS) -o $@ $< $(LDLIBS)

clean:
	rm -f $(TARGET)

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

.PHONY: clean install service service-uninstall service-usb service-usb-uninstall

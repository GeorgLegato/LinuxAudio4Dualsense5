# ds5_membrane_sink — nativer PipeWire-Sink fuer den DualSense-Membran-Speaker
#
# Voraussetzungen:
#   sudo apt install libpipewire-0.3-dev libopus-dev zlib1g-dev gcc make

CC      ?= gcc
CFLAGS  ?= -O2 -Wall -Wextra
PKGS     = libpipewire-0.3 opus zlib
CFLAGS  += $(shell pkg-config --cflags $(PKGS))
LDLIBS   = $(shell pkg-config --libs $(PKGS))

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

.PHONY: clean install service service-uninstall

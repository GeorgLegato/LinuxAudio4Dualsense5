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

.PHONY: clean install

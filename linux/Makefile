# Weitblick Remote for Linux
#
#   make            builds the C bridge (build/libweitblick-bridge.so), the icons and the German translation
#   make test       runs the unit tests
#   make run        starts the app from the source tree
#   make install    installs into ~/.local (PREFIX=/usr/local for a system install, needs root)
#   make deb        builds the Debian package into build/deb/
#   make flatpak    builds the Flatpak bundle into build/flatpak/ (flatpak-builder; fetches the GNOME SDK)
#
# The C bridge and the app icon are the macOS app's: ../Sources/WeitblickBridge and ../design/icon.
# Build dependencies: a C compiler and the FreeRDP 3 headers (sudo apt install freerdp3-dev libwinpr3-dev).
# Without root, `make headers` downloads the two -dev packages and unpacks them into build/sysroot.

PREFIX   ?= $(HOME)/.local
CC       ?= cc
PYTHON   ?= python3
BUILD    := build
SYSROOT  := $(BUILD)/sysroot
BRIDGE   := ../Sources/WeitblickBridge
ICON_SRC := ../design/icon
APP_ID   := nrw.neuhaus.WeitblickRemote

# Headers: pkg-config if the -dev packages are installed, else the unpacked copies in build/sysroot.
FREERDP_CFLAGS := $(subst -I,-isystem ,$(shell pkg-config --cflags freerdp3 freerdp-client3 winpr3 2>/dev/null))
ifeq ($(strip $(FREERDP_CFLAGS)),)
FREERDP_CFLAGS := -isystem $(SYSROOT)/usr/include/freerdp3 -isystem $(SYSROOT)/usr/include/winpr3
endif
# Links against the runtime libraries (libfreerdp3-3 etc.), which have no unversioned .so link.
FREERDP_LIBS := $(shell pkg-config --libs freerdp3 freerdp-client3 winpr3 2>/dev/null)
ifeq ($(strip $(FREERDP_LIBS)),)
FREERDP_LIBS := -l:libfreerdp-client3.so.3 -l:libfreerdp3.so.3 -l:libwinpr3.so.3
endif

CFLAGS   ?= -O2 -g
CFLAGS   += -std=gnu17 -fPIC -fvisibility=default -Wall -Wextra -Wno-unused-parameter -I$(BRIDGE)/include
LDFLAGS  += -shared -Wl,--as-needed -Wl,-z,defs

BRIDGE_SRC := $(wildcard $(BRIDGE)/*.c)
BRIDGE_OBJ := $(BRIDGE_SRC:$(BRIDGE)/%.c=$(BUILD)/obj/%.o)
BRIDGE_LIB := $(BUILD)/libweitblick-bridge.so
MO_FILES   := $(patsubst po/%.po,$(BUILD)/locale/%/LC_MESSAGES/weitblick-remote.mo,$(wildcard po/*.po))
ICONS      := $(BUILD)/icons/hicolor

.PHONY: all headers test run install uninstall deb flatpak clean

all: $(BRIDGE_LIB) $(BUILD)/icons.stamp $(MO_FILES)

$(BUILD)/obj/%.o: $(BRIDGE)/%.c $(BRIDGE)/wb_internal.h $(BRIDGE)/include/WeitblickBridge.h
	@mkdir -p $(dir $@)
	@test -n "$$(pkg-config --cflags freerdp3 2>/dev/null)" || test -d $(SYSROOT)/usr/include/freerdp3 || \
		{ echo "FreeRDP headers missing: sudo apt install freerdp3-dev libwinpr3-dev, or run 'make headers'"; exit 1; }
	$(CC) $(CFLAGS) $(FREERDP_CFLAGS) -c $< -o $@

$(BRIDGE_LIB): $(BRIDGE_OBJ)
	$(CC) $(LDFLAGS) -o $@ $^ $(FREERDP_LIBS) -lpthread

# The app icon in the hicolor sizes (from the macOS app's exports) and the Linux-only symbolic icons.
$(BUILD)/icons.stamp: $(ICON_SRC)/app-icon.svg $(wildcard $(ICON_SRC)/export/*.png data/icons/hicolor/*/apps/*)
	rm -rf $(BUILD)/icons
	mkdir -p $(BUILD)/icons
	cp -r data/icons/hicolor $(BUILD)/icons/
	for size in 16 32 48; do \
		install -Dm644 $(ICON_SRC)/export/favicon-$$size.png $(ICONS)/$${size}x$${size}/apps/$(APP_ID).png; done
	for size in 128 256 512; do \
		install -Dm644 $(ICON_SRC)/export/icon-$$size.png $(ICONS)/$${size}x$${size}/apps/$(APP_ID).png; done
	install -Dm644 $(ICON_SRC)/app-icon.svg $(ICONS)/scalable/apps/$(APP_ID).svg
	touch $@

$(BUILD)/locale/%/LC_MESSAGES/weitblick-remote.mo: po/%.po scripts/msgfmt.py
	@mkdir -p $(dir $@)
	$(PYTHON) scripts/msgfmt.py $< $@

headers:
	scripts/fetch-freerdp-headers.sh $(SYSROOT)

test: $(BRIDGE_LIB)
	env -u DISPLAY -u WAYLAND_DISPLAY $(PYTHON) -m unittest discover -s tests -t . $(if $(V),-v,)

run: all
	$(PYTHON) -m weitblick

install: all
	DESTDIR="$(DESTDIR)" scripts/install.sh "$(PREFIX)"

uninstall:
	scripts/install.sh --uninstall "$(PREFIX)"

deb: all
	scripts/build-deb.sh

# Builds from this checkout (the manifest's sources are ../.. and skip build/).
flatpak:
	flatpak-builder --user --install-deps-from=flathub --force-clean --state-dir=$(BUILD)/flatpak/state \
		--repo=$(BUILD)/flatpak/repo $(BUILD)/flatpak/app flatpak/$(APP_ID).yml
	flatpak build-bundle $(BUILD)/flatpak/repo $(BUILD)/flatpak/Weitblick-Remote.flatpak $(APP_ID) \
		--runtime-repo=https://flathub.org/repo/flathub.flatpakrepo

clean:
	rm -rf $(BUILD)/obj $(BRIDGE_LIB) $(BUILD)/locale $(BUILD)/icons $(BUILD)/icons.stamp $(BUILD)/deb $(BUILD)/flatpak

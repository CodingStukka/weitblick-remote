# Third-party notices

Weitblick Remote for Linux is licensed under the Apache License, Version 2.0 (see LICENSE). It is the Linux
version of [Weitblick Remote for macOS](https://github.com/mneuhaus/weitblick-remote) by Marc Neuhaus (Apache-2.0):
it builds the macOS app's C bridge (`Sources/WeitblickBridge`) and uses its icon; the rest is a new implementation
of its design.

The port distributes no third-party code. At run time it uses the system's libraries:

- **FreeRDP 3 and WinPR** (https://www.freerdp.com), Apache License 2.0 – the RDP protocol, codecs and the
  clipboard, audio, drive and printer channels.
- **GTK 4, libadwaita, GLib, GdkPixbuf, libsecret** (https://gtk.org, https://gnome.org), GNU LGPL 2.1 or later.
- **PyGObject** (https://pygobject.gnome.org), GNU LGPL 2.1 or later.
- **Tesseract OCR** (optional, https://github.com/tesseract-ocr/tesseract), Apache License 2.0 – on-screen text
  recognition for AI agents, called as a separate program when installed.

The app icon is the one of the macOS app (Apache-2.0).

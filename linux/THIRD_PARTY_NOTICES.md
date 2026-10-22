# Third-party notices

Weitblick Remote for Linux is licensed under the Apache License, Version 2.0 (see LICENSE). It is the Linux
version of [Weitblick Remote for macOS](https://github.com/mneuhaus/weitblick-remote) by Marc Neuhaus (Apache-2.0):
it builds the macOS app's C bridge (`Sources/WeitblickBridge`) and uses its icon; the rest is a new implementation
of its design.

The Debian package distributes no third-party code. At run time it uses the system's libraries:

- **FreeRDP 3 and WinPR** (https://www.freerdp.com), Apache License 2.0 – the RDP protocol, codecs and the
  clipboard, audio, drive and printer channels.
- **GTK 4, libadwaita, GLib, GdkPixbuf, libsecret** (https://gtk.org, https://gnome.org), GNU LGPL 2.1 or later.
- **PyGObject** (https://pygobject.gnome.org), GNU LGPL 2.1 or later.
- **Tesseract OCR** (optional, https://github.com/tesseract-ocr/tesseract), Apache License 2.0 – on-screen text
  recognition for AI agents, called as a separate program when installed.

The Flatpak bundle (`flatpak/nrw.neuhaus.WeitblickRemote.yml`) runs on the GNOME runtime from Flathub, which
provides GTK, libadwaita, GLib, GdkPixbuf, libsecret, PyGObject and Python, and bundles:

- **FreeRDP 3 and WinPR** 3.32.1 (https://www.freerdp.com), Apache License 2.0.
- **Tesseract OCR** 5.5.3 (https://github.com/tesseract-ocr/tesseract), Apache License 2.0, with the English and
  German models of **tessdata_fast** 4.1.0 (https://github.com/tesseract-ocr/tessdata_fast), Apache License 2.0.
- **Leptonica** 1.87.0 (http://www.leptonica.org), BSD 2-Clause license (Tesseract's image library).

The app icon is the one of the macOS app (Apache-2.0).

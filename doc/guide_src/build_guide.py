# -*- coding: utf-8 -*-
"""Build the Windows setup guide from one source into every place it is read.

    python doc/guide_src/build_guide.py [path/to/dps_zkteco_biometric_integration]

The guide is read in three places -- sent to the customer as a file, opened from
the bridge form in Odoo, and opened from the bridge's own local page -- and
three hand-edited copies would drift apart within a release. Edit body.html and
img/ here, then run this.

Outputs:
  doc/WINDOWS_SETUP_ar.html                       the file sent to a customer
  src/zkbridge/web/static/guide_ar.html            the bridge's own Guide page
  <module>/static/doc/bridge_windows_setup_ar.html  opened from Odoo, when the
                                                    module path is given
"""

import base64
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
TITLE = 'جسر ZKTeco — التشغيل على ويندوز'
FONTS = ('<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Readex+Pro:wght@400;600'
         '&family=IBM+Plex+Sans+Arabic:wght@400;600&family=IBM+Plex+Mono:wght@400&display=swap">\n')


def render(with_web_fonts):
    """The full page, images inlined so it travels as one file.

    The bridge's copy carries no web fonts: its page forbids every outside
    request, and the PC may have no route to Google anyway -- the system Arabic
    fonts are a perfectly good fallback.
    """
    body = (HERE / 'body.html').read_text(encoding='utf-8')
    for key in sorted(set(re.findall(r'IMG:(\w+)', body))):
        data = base64.b64encode((HERE / 'img' / (key + '.png')).read_bytes()).decode()
        body = body.replace('IMG:%s"' % key, 'data:image/png;base64,%s"' % data)
    if 'IMG:' in body:
        raise SystemExit('an image referenced in body.html is missing from img/')
    return ('<!doctype html>\n<html lang="ar" dir="rtl">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            '<title>%s</title>\n%s</head>\n<body>\n%s\n</body>\n</html>\n'
            % (TITLE, FONTS if with_web_fonts else '', body))


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    print('%-80s %5.0f KB' % (path, path.stat().st_size / 1024))


def main(argv):
    write(REPO / 'doc' / 'WINDOWS_SETUP_ar.html', render(True))
    write(REPO / 'src' / 'zkbridge' / 'web' / 'static' / 'guide_ar.html', render(False))
    if len(argv) > 1:
        module = pathlib.Path(argv[1]).resolve()
        if not (module / '__manifest__.py').exists():
            raise SystemExit('%s is not an Odoo module' % module)
        write(module / 'static' / 'doc' / 'bridge_windows_setup_ar.html', render(True))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))

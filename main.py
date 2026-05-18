import sys
import os
import threading
import webbrowser
import socket
import time


def is_port_open(port):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            s.connect(('127.0.0.1', port))
            return True
    except Exception:
        return False


def find_free_port(start=8000, end=9000):
    for port in range(start, end):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(('127.0.0.1', port))
                return port
        except OSError:
            continue
    return start


def wait_and_open_browser(port):
    for _ in range(40):
        if is_port_open(port):
            break
        time.sleep(0.25)
    webbrowser.open(f'http://127.0.0.1:{port}')


def create_tray_icon(port):
    from PIL import Image, ImageDraw
    import pystray

    size = 64
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    c_light = (52, 152, 219)
    c_dark  = (41, 128, 185)
    c_edge  = (21, 67, 96)

    draw.rectangle([6, 14, 58, 52], fill=c_dark)
    draw.ellipse([6, 44, 58, 58], fill=c_dark, outline=c_edge, width=1)
    draw.ellipse([6, 28, 58, 42], fill=c_light, outline=c_edge, width=1)
    draw.ellipse([6, 4, 58, 18],  fill=c_light, outline=c_edge, width=1)

    def on_open(icon, item):
        webbrowser.open(f'http://127.0.0.1:{port}')

    def on_quit(icon, item):
        icon.stop()
        os._exit(0)

    menu = pystray.Menu(
        pystray.MenuItem('เปิดโปรแกรม', on_open, default=True),
        pystray.MenuItem('ปิดโปรแกรม', on_quit),
    )
    return pystray.Icon('DB Compare Sync', img, 'DB Compare & Sync Tool', menu)


if __name__ == '__main__':
    from app import app

    if getattr(sys, 'frozen', False):
        # Already running — just open browser
        if is_port_open(8000):
            webbrowser.open('http://127.0.0.1:8000')
            sys.exit(0)

        port = find_free_port(8000)

        flask_thread = threading.Thread(
            target=lambda: app.run(
                debug=False, port=port, host='127.0.0.1',
                use_reloader=False, threaded=True
            ),
            daemon=True,
        )
        flask_thread.start()

        threading.Thread(target=wait_and_open_browser, args=(port,), daemon=True).start()

        tray = create_tray_icon(port)
        tray.run()
    else:
        app.run(debug=True, port=8000, host='0.0.0.0')

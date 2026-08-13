import tkinter as tk
from ui.ping_window import PingApp


def main():

    root = tk.Tk()

    PingApp(root)

    root.mainloop()


if __name__ == "__main__":
    main()
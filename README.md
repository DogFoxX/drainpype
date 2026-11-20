# DrainPype File Uploader

<img align="left" hspace="15" src="uploader/assets/icon.ico" alt="DrainPype icon" height="200">
<h3>A desktop and command-line, multi-server, multi-file uploading tool.</h3>
<h4>DrainPype sits between what you feed it, and the server you want to upload to.</h4>
<a href="https://github.com/DogFoxX/drainpype/releases/latest"><img src="https://img.shields.io/github/v/release/DogFoxX/drainpype?style=for-the-badge&label=DrainPype&labelColor=2F3541&color=DBA26A"></a>
<br clear="left"/>

> [!IMPORTANT]
> This project was built with the help of AI, from a small original idea.
> First iteration was just a basic Python gui, with a server selector, file picker, and an upload button.
> I have limited knowledge of Python, and this project serves as a way for me to learn more.

## Features

- Upload single or multiple files.
- Queueing with max concurrent uploads.
- Hosting service (server) selection for each upload.
- Add custom JSON or YAML server definitions. See the [server definition guide](docs/SERVERS.md) and [JSON schema](docs/server.schema.json).
- Save API keys for each server.

> [!IMPORTANT]
> API Keys are stored in `%APPDATA%/drainpype/keys` on Windows and `~/.config/drainpype/keys` on Linux.
> Saved keys are reversibly obfuscated, not encrypted.
> Encryption options are being explored.

## Included servers

The default server definitions include:

- **[Pixeldrain](https://pixeldrain.com)** — API key required.
- **[fuckingfast.net](https://fuckingfast.net)** — optional location, note, account, and directory options.
- **[Gofile](https://gofile.io)** — guest uploads, with optional account and folder options.
- **[Google Drive](https://drive.google.com)** — Google OAuth2 authorization, and optional folder.

> [!NOTE]
>
> - DrainPype uses Google OAuth2 to authorize and upload files to your Google Drive.
> - You sign in directly with your Google Account; DrainPype never receives your Google password.
> - DrainPype requests only the Google Drive permission (`https://www.googleapis.com/auth/drive.file`) shown on Google's authorization consent screen.
> - It does not request access to Gmail, Contacts, Calendar, or unrelated Google data.

## Download and Run

- [Windows](https://github.com/DogFoxX/drainpype/releases/latest/download/drainpype-windows.zip)

- [Linux](https://github.com/DogFoxX/drainpype/releases/latest/download/drainpype-linux.zip)

```shell
# First make the Linux binary executable
chmod +x ./drainpype
```

## Command line

Run `drainpype --help` for the complete list of options. For example, upload two files to Gofile with up to two concurrent uploads:

```shell
drainpype --server gofile --file archive.zip --file video.mkv --max-uploads 2
```

Useful options:

| Option                     | Description                                                     |
| -------------------------- | --------------------------------------------------------------- |
| `-F`, `--file FILE`        | File to upload; repeat to add multiple files.                   |
| `-S`, `--server SERVER`    | Server definition ID. The default is Pixeldrain when available. |
| `-K`, `--key KEY`          | API key or token for the selected server.                       |
| `-o`, `--option KEY=VALUE` | Set a server option; repeat as needed.                          |
| `-m`, `--max-uploads N`    | Maximum concurrent uploads.                                     |
| `--auth`                   | Start OAuth authorization for the selected server and exit.     |
| `--list-servers`           | List available server definitions and exit.                     |
| `-v`, `--version`          | Print the installed version.                                    |

For example, set a server option and upload a file:

```shell
drainpype -S ffast -F archive.zip
```

## Build from source

### Requirements

- Python 3.10 or newer
- `curl` on `PATH`
- `pip` (included with current Python installations)
- PySide6, installed automatically from the project dependencies
- PyYAML only if you use YAML server definitions

On Windows 10/11, install Python from [python.org](https://www.python.org/downloads/) and enable the option to add Python to `PATH`. `curl.exe` is included with current Windows versions.

#### Debian / Ubuntu:

```shell
sudo apt update
sudo apt install python3 python3-pip python3-venv curl
```

#### Arch Linux:

```shell
sudo pacman -S --needed python python-pip curl
```

### Install a working copy

Clone the repository, then from its root directory install DrainPype in editable mode:

```shell
git clone https://github.com/DogFoxX/drainpipe.git
cd drainpipe
python -m pip install -e .
```

The package metadata installs the required Python dependency, PySide6. If you need YAML definitions, install the optional extra too:

```shell
python -m pip install -e '.[yaml]'
```

Launch the GUI:

```shell
python -m uploader
```

Run the CLI:

```shell
python -m uploader --help
python -m uploader --list-servers
```

### Build a standalone executable (optional)

Install PyInstaller in the active environment:

```shell
python -m pip install pyinstaller
```

> [!NOTE]
> To enable Google Drive in the executable, provide your own Google OAuth client file at `uploader/assets/google_oauth_client.json` before building.
>
> The spec bundles the `uploader/assets` directory, so do not commit this personal file.

From the repository root, build the GUI executable:

```shell
pyinstaller drainpype.spec
```

The executable is written under `dist/`. On Windows, the spec configures the application icon and hides the console window.

## Privacy and Responsibility

Uploads go directly to the selected hosting service. DrainPype does not send file contents or usage telemetry to the project or it's developers. Hosting services may log requests or collect information according to their own policies. You are responsible for ensuring that you have the right to upload your files and for complying with each service's terms.

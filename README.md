# QuarkDownload

A small collection of Python downloaders for public cloud-drive shares:

- **`downloader.py`** — downloads whole folders from **Quark Pan** (`pan.quark.cn`)
  public share links anonymously, with recursive enumeration, resume, retries,
  concurrency and an optional [aria2c](https://aria2.github.io/) backend. It also
  detects **Baidu Pan** links and can fall back to an alternate provider URL.
- **`redguo_downloader.py`** — downloads **RedGuo / Hongguo (红果短剧)** short-drama
  episodes through the anonymous playback API, including CENC decryption. Useful
  for episodes that are paywalled or size-capped on other platforms.

Both tools are designed to run without a user account wherever the provider
allows it.

## Features

### `downloader.py` (Quark / Baidu)

- Auto-detects the provider from the share URL.
- Anonymous Quark public-share flow (no login): obtains the anonymous share
  token, recursively enumerates all folders and paginates until exhausted.
- Preserves the original folder structure.
- `--enumerate-only` prints total folders, files and size without downloading.
- Test-downloads the smallest video first and validates the response is real
  media (not an HTML/login page).
- Resumable downloads using `.part` files and HTTP `Range`; completed files are
  skipped, so re-running the same command resumes.
- Exponential-backoff retries; refreshes expired temporary download URLs.
- Conservative concurrency via `--workers`, plus optional `--aria2` for
  multi-connection downloads of large files.
- Authenticated fallback for Quark files above the anonymous ~50 MB cap
  (error `23018`) when a logged-in cookie is supplied.
- Writes a `manifest.json` inventory, `failed.json` and a `download.log`.
- For Baidu links: probes the public endpoint, reports that anonymous download
  is not supported, and can automatically fall back to `--alt-url` (typically a
  Quark link for the same content).

### `redguo_downloader.py` (RedGuo / Hongguo)

- Resolves a series from a `hongguoduanju.com` detail/player URL or a raw
  `series_id`.
- Uses the anonymous playback API and downloads the CENC
  (`cenc-aes-ctr`) encrypted media, derives the content key, and decrypts it
  with a bundled ffmpeg build.
- Quality selection (`--quality`, 360–1080), episode ranges (`--episodes`),
  resume/skip and concurrency (`--workers`).
- No account required.

## Requirements

- **Python 3.9+**
- Python packages (see `requirements.txt`):
  - `requests`
  - `pycryptodome` (CENC content-key derivation for `redguo_downloader.py`)
  - `imageio-ffmpeg` (downloads a bundled ffmpeg used for decryption)
- Optional for `downloader.py --aria2`: **aria2c** available on `PATH`
  (recommended for higher throughput on large shares).

`ffmpeg` does not need to be installed manually; `imageio-ffmpeg` provides a
binary for the RedGuo tool.

## Installation

```bash
git clone git@github.com:willtanoe/quarkdownload.git
cd quarkdownload
python -m pip install -r requirements.txt
```

## Usage

### Quark public share

```bash
python downloader.py "https://pan.quark.cn/s/0123456789ab" --output "./downloads"
```

### Quark share with password

```bash
python downloader.py "https://pan.quark.cn/s/0123456789ab" --pwd "1234" --output "./downloads"
```

### Faster downloads with aria2c and more workers

```bash
python downloader.py "https://pan.quark.cn/s/0123456789ab" --output "./downloads" --workers 6 --aria2
```

### Enumerate only (no download)

```bash
python downloader.py "https://pan.quark.cn/s/0123456789ab" --enumerate-only
```

### Test a single file and stop

```bash
python downloader.py "https://pan.quark.cn/s/0123456789ab" --test
```

### Quark link with a Baidu fallback

```bash
python downloader.py "https://pan.quark.cn/s/0123456789ab" \
  --alt-url "https://pan.baidu.com/s/xxxx?pwd=abcd" --pwd "abcd" \
  --output "./downloads"
```

### Download a RedGuo series

```bash
python redguo_downloader.py "https://hongguoduanju.com/detail?series_id=7684943340281793560" \
  --output "./downloads" --workers 3 --quality 1080
```

### RedGuo: single episode or a range

```bash
python redguo_downloader.py "https://hongguoduanju.com/detail?series_id=7683340281793560" --episodes 8
python redguo_downloader.py "https://hongguoduanju.com/detail?series_id=7683340281793560" --episodes 1-20
```

Run `python downloader.py --help` or `python redguo_downloader.py --help` for the
complete option list.

## Project Structure

```text
quarkdownload/
├── downloader.py          # Quark / Baidu public-share downloader
├── redguo_downloader.py   # RedGuo / Hongguo short-drama downloader
├── requirements.txt       # Python dependencies
├── tests/
│   └── test_smoke.py      # offline CLI smoke tests (stdlib unittest)
├── README.md
└── .gitignore
```

Runtime files created next to the scripts (and ignored by Git):
`downloads/`, `manifest.json`, `failed.json`, `download.log`.

## Configuration

### Quark cookie (optional)

Anonymous Quark guests are limited to roughly **50 MB per file**. Larger files
return the error `download file size limit` (code `23018`). To download them,
supply a logged-in Quark cookie; the tool then uses the authenticated
save-and-download flow, which lifts the cap.

The cookie is read from the first available source:

1. `--cookie "<COOKIE>"`
2. `--cookie-file <PATH>`
3. the `QUARK_COOKIE` environment variable
4. `cookie.txt` in the project directory

```bash
python downloader.py "https://pan.quark.cn/s/0123456789ab" --cookie "<COOKIE>" --output "./downloads"
```

Get the cookie value from a browser while logged into `pan.quark.cn`
(Developer Tools → Network → any `drive-pc.quark.cn` request → Request Headers →
`Cookie`). Do not commit cookies or `cookie.txt`; they are git-ignored.

No `.env` file is loaded automatically — set `QUARK_COOKIE` in your shell if you
prefer the environment variable.

## Output

- Files are written under the `--output` directory (default `./downloads`),
  preserving the share's folder structure. Cached names use the special
  characters `\ / : * ? " < > |` replaced with `_` for Windows compatibility.
- `manifest.json` (next to the script) lists every discovered file with its
  provider, share URL, name, relative path, remote file ID, expected size,
  downloaded size and status.
- `failed.json` lists files that failed, with their error messages.
- `download.log` records the full run.
- Interrupted downloads leave `.part` files and resume on the next run.

## Development

Run the offline smoke tests (no network access required):

```bash
python -m unittest discover -s tests -v
```

The tests compile both scripts, exercise `--help`, and check provider detection.
No linter or formatter is configured for this project.

## Notes / Limitations

- **Quark guest cap:** anonymous downloads are limited to ~50 MB per file; a
  logged-in cookie is required for larger files.
- **Baidu Pan:** anonymous listing/download of shared files is blocked by the
  provider (`errno -9`); a Baidu link is only probed and reported. Use a Quark
  link (optionally via `--alt-url`) for actual downloads.
- **RedGuo:** relies on a public detail page and playback API that may change or
  rate-limit; episode availability is controlled by the provider.
- **aria2c** is not bundled; install it separately if you want `--aria2`.
- Download speed for anonymous Quark shares is throttled by the provider and can
  vary; `--aria2` with several workers generally improves aggregate throughput.

## Disclaimer

This project is provided for personal and educational use. Downloading content
may be subject to copyright, the terms of service of the respective platforms,
and local law. You are solely responsible for ensuring that your use complies
with all applicable terms, licenses and legislation. The authors do not host,
provide or endorse any content and are not affiliated with Quark, Baidu, RedGuo
or any other provider mentioned here.

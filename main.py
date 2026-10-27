# ASSIST (assist.dla.mil) PDF downloader - never-stop version: no arguments, every error is logged and skipped.
# VERBOSE LOGGING: everything that happens is logged to the console (default level: DEBUG).
# Everything (pages, login checks and PDF downloads) goes through Chrome, so only the browser has to be logged in.
# NO URL TWICE: a document that an earlier run finished is not opened again (RECHECK_KNOWN_DOCUMENTS = False),
# saved files are never requested again, and no extra "probe" or "keep-alive" requests are made.
# The only repeated requests are RETRIES of requests that failed, and re-opening a results page after a Chrome restart.
# Any Chrome problem (crash, closed window, logout, broken results page) restarts Chrome, logs in again and resumes.
# DOWNLOADS: every PDF is fetched INSIDE Chrome (same cookies, same network stack) with a hard timeout and checked
# for completeness (size, %PDF start); a file without %%EOF is saved as ".suspect" and never requested again.
# MULTI-THREADED: the main thread owns the ONE Chrome window and walks the results pages; WORKER_COUNT worker
# threads each connect to that same Chrome over a local debugging port and process documents in their OWN TABS.
# Requires Python 3.13 or newer (it uses the `type` statement).

# ===========================================================================
# IMPORTS
# ===========================================================================
# Decodes the PDF pieces that Chrome hands over as base64 text
import base64

# Stops the background ping processes when the script ends
import atexit

# Recognises the "disk is full" operating system error number
import errno

# Computes a SHA-256 fingerprint of every saved PDF
import hashlib

# Reads Chrome's settings file and safely quotes strings for JavaScript
import json

# Writes the console log
import logging

# Operating system name and environment variables
import os

# Hands typed keyboard lines from the keyboard thread to the main thread
import queue

# Random jitter for the pauses between requests
import random

# Regular expressions for text pattern matching
import re

# Measures free disk space, finds programs on the PATH and deletes folders
import shutil

# Finds a free local port for Chrome's remote-debugging connection
import socket

# Local database of everything seen and saved in earlier runs
import sqlite3

# Starts the background ping processes
import subprocess

# Standard input and output streams, platform name and stack frames
import sys

# Keyboard reader thread, network watchdog thread and locks
import threading

# Pauses and elapsed-time measurement
import time

# Type hints for callback functions and generators
from collections.abc import Callable, Iterator

# Lightweight classes that only hold data
from dataclasses import dataclass, field

# Date formatting, time differences and time zones (timezone is needed to read the Retry-After date correctly)
from datetime import datetime, timedelta, timezone

# Parses HTTP dates (used for the Retry-After header)
from email.utils import parsedate_to_datetime

# Cross-platform file paths
from pathlib import Path

# Type hints
from typing import Any, TypedDict

# URL parsing helpers
from urllib.parse import parse_qs, urljoin, urlparse

# psutil is optional: it is only needed to stop Chrome when the internet goes down
try:
    # Finds and stops the Chrome processes that belong to this script
    import psutil
# psutil is not installed
except ImportError:
    # The network watchdog then cannot stop Chrome by itself
    psutil = None

# Catches lxml parsing errors precisely
from lxml import etree

# Fast HTML parsing
from lxml import html as lxml_html

# Playwright types used in annotations
from playwright.sync_api import (
    BrowserContext,
    Dialog,
    Download,
    Locator,
    Page,
    Playwright,
    Response,
)

# Playwright's own error class (network errors, timeouts, closed browser)
from playwright.sync_api import Error as PlaywrightError

# Starts and controls the Chromium browser
from playwright.sync_api import sync_playwright

# ===========================================================================
# TYPES
# ===========================================================================
# One attempt of a retried operation (it is always called with attempt_label=...)
type AttemptFunction = Callable[..., Any]

# A function that checks a downloaded page (it is always called with page_body=...)
type PageValidator = Callable[..., None]


# One row of the search results grid
class DocumentRecord(TypedDict):
    # Internal document ID (used in the document page URL)
    internal_id: str
    # Public document ID (used as the folder name)
    public_id: str


# One PDF link found on a document page
class PdfLink(TypedDict):
    # Absolute download URL
    download_url: str
    # File number taken from the "dmxid" URL parameter
    file_number: str
    # Description text from the document page
    description: str
    # Date text from the document page
    date_text: str


# Immutable record of one problem that happened during the run
@dataclass(frozen=True, slots=True, kw_only=True)
class ProblemRecord:
    # When the problem happened
    timestamp: str
    # Which document or file it belongs to
    label: str
    # The address (or file path) involved
    url: str
    # What went wrong
    reason: str


# Immutable record of a document that is still failing and will be retried
@dataclass(frozen=True, slots=True, kw_only=True)
class FailingDocument:
    # The results-grid row of the document
    record: DocumentRecord
    # The ORIGINAL log label (retry suffixes are only added when logging)
    label: str
    # How many times the document has failed so far
    attempts: int


# Immutable result of one finished PDF download
@dataclass(frozen=True, slots=True, kw_only=True)
class DownloadedPdf:
    # Number of bytes on disk
    size_bytes: int
    # Full SHA-256 of the file as hexadecimal text
    sha256_hex: str
    # True if the file ends with %%EOF (looks complete)
    has_end_marker: bool
    # The temporary file that holds the data
    partial_path: Path


# ===========================================================================
# SETTINGS
# ===========================================================================
# Default is DEBUG: show everything (use logging.INFO for a quieter log)
LOG_LEVEL = logging.DEBUG
# Sleeps shorter than this are not logged (hides the short polling loops)
LOG_SLEEPS_FROM_SECONDS = 1.0
# False: a document that an earlier run finished is NOT opened again (no URL twice); True: open every document again
RECHECK_KNOWN_DOCUMENTS = False
# False: no periodic keep-alive request (it would request the same URL again and again)
KEEP_ALIVE_ENABLED = False
# Normal wait in seconds between web requests
SECONDS_BETWEEN_REQUESTS = 0.5
# The adaptive delay never grows beyond this many seconds
MAXIMUM_DELAY_SECONDS = 10
# Every pause is multiplied by a random factor of at least this
JITTER_MINIMUM_FACTOR = 0.7
# ... and at most this
JITTER_MAXIMUM_FACTOR = 1.3
# True makes the browser window visible
SHOW_BROWSER_WINDOW = True
# True on Linux (Ubuntu, Debian, ...), where Chrome would otherwise use the GNOME key-ring / KWallet
IS_LINUX = sys.platform.startswith("linux")
# Chrome opens maximized and never shows the "Restore pages?" bubble or error dialogs
BROWSER_LAUNCH_ARGUMENTS = [
    "--start-maximized",
    "--disable-session-crashed-bubble",
    "--hide-crash-restore-bubble",
    "--noerrdialogs",
    # Linux only: store passwords and cookies in the profile folder instead of the system key-ring
    *(["--password-store=basic"] if IS_LINUX else []),
]
# Playwright default flags that are removed so Chrome's password manager still works
BROWSER_IGNORED_DEFAULT_ARGUMENTS = (
    # Linux: only the automation banner flag is removed; the key-ring stays disabled
    ["--enable-automation"]
    if IS_LINUX
    # Windows and macOS keep the old behaviour
    else ["--enable-automation", "--password-store=basic", "--use-mock-keychain"]
)
# How long to wait for Chrome to autofill the saved username and password
AUTO_LOGIN_WAIT_SECONDS = 15
# Timeout in milliseconds for each automatic click or wait
AUTO_STEP_TIMEOUT_MS = 30_000
# Timeout in milliseconds for the optional "Search Method" and "Advanced Search" clicks
AUTO_OPTIONAL_CLICK_TIMEOUT_MS = 5_000
# Pause the run while less than 500 MB of disk space is free
MINIMUM_FREE_DISK_BYTES = 500 * 1024 * 1024
# Any "PDF" smaller than this many bytes is treated as broken
MINIMUM_PDF_BYTES = 1
# Never wait longer than 5 minutes because a server asked us to slow down
MAXIMUM_RETRY_AFTER_SECONDS = 300
# Seconds between free-disk-space checks while the disk is too full
DISK_WAIT_SECONDS = 30
# Base wait in seconds before the browser session restarts after a crash
RESTART_WAIT_SECONDS = 10
# The restart wait never grows beyond 5 minutes
MAXIMUM_RESTART_WAIT_SECONDS = 300
# Waits between paging attempts and breaker pauses never grow beyond 5 minutes
MAXIMUM_BACKOFF_SECONDS = 300
# Paging failures in a row (without reaching a new results page) that restart Chrome before the run stops
MAXIMUM_PAGING_RESTARTS = 5
# Lower-case Playwright error texts that mean Chrome is unusable
FATAL_BROWSER_ERROR_MARKERS = (
    "has been closed",
    "target closed",
    "crashed",
    "browser closed",
    "connection closed",
    "disconnected",
)
# Lower-case Chrome error text of a redirect loop (the session cookies are invalid)
REDIRECT_LOOP_MARKER = "err_too_many_redirects"
# Extra rounds spent on documents that failed (every retry costs requests)
FINAL_RETRY_ROUNDS = 1
# When the first ping address is silent this long, the backup address is pinged too
PING_BACKUP_AFTER_SECONDS = 3
# No ping reply for this long means the internet is down (Chrome is then stopped)
PING_SILENCE_SECONDS = 60
# When ping gets no reply, wait this long and then restart ping
PING_RETRY_WAIT_SECONDS = 60
# After ping was (re)started, wait this long for the first reply
PING_FIRST_REPLY_WAIT_SECONDS = 10
# How often the background watchdog looks at the ping replies
PING_CHECK_SECONDS = 2
# A ping process that exited (for example "network unreachable") is restarted at most this often
PING_RESTART_MIN_SECONDS = 5
# The browser requests a small page this often (only when KEEP_ALIVE_ENABLED is True)
KEEP_ALIVE_SECONDS = 240
# This many failed documents in a row pauses the run
CIRCUIT_BREAKER_FAILURES = 5
# First breaker pause in seconds (doubles with every trip in a row)
CIRCUIT_BREAKER_BASE_SECONDS = 30
# A waiting prompt is shown (and signalled) again every 10 minutes
PROMPT_REPEAT_SECONDS = 600
# A file the server refused is not requested again for this many hours (only used when RECHECK_KNOWN_DOCUMENTS is True)
REFUSED_RECHECK_HOURS = 24
# How many items the final summaries list
MAXIMUM_ITEMS_SHOWN = 100
# Number of bytes in one megabyte
BYTES_PER_MEGABYTE = 1024 * 1024
# Number of times to retry a failed operation
RETRY_ATTEMPTS = 3
# Base backoff time in seconds for retries
SECONDS_PER_RETRY_STEP = 2
# Print the running totals after this many documents
PROGRESS_EVERY_N_DOCUMENTS = 1
# True: after a restart, continue at the results page saved in the database (False: always start at page 1)
RESUME_FROM_SAVED_PAGE = True
# Name of the row in the resume table
RESUME_KEY = "results_page"
# While paging forward to the saved page, log every this many pages
PAGES_BETWEEN_SKIP_LOGS = 10
# Rounds of attempts to page to or read a results page before Chrome is restarted
PAGING_MAXIMUM_ROUNDS = 5
# Failed documents are retried after every this many results pages
RETRY_FAILED_EVERY_N_PAGES = 5
# Failed documents are retried during the run until they failed this often (the final round tries all)
MAXIMUM_DOCUMENT_ATTEMPTS = 3
# Network timeout in milliseconds
REQUEST_TIMEOUT_MS = 120_000
# Polling interval for keyboard input in milliseconds
KEYBOARD_POLL_MS = 300
# If terminal input is closed, prompts continue by themselves after this many seconds
CLOSED_INPUT_WAIT_SECONDS = 5
# Log line layout: time with milliseconds, level, thread name, function name, message
LOG_LINE_FORMAT = "%(asctime)s.%(msecs)03d [%(levelname)s] [%(threadName)s] %(funcName)s: %(message)s"
# Layout of the time in a log line (milliseconds are added by the format above)
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
# Piece size when hashing a saved PDF
HASH_CHUNK_BYTES = 1024 * 1024
# How much of a "not a PDF" file is read to recognise a login page
MAXIMUM_NON_PDF_BYTES = 2 * 1024 * 1024
# On Windows, file paths are kept below this length (the hard limit is 260)
MAXIMUM_WINDOWS_PATH_LENGTH = 240
# The description part of a file name is never shortened below this many characters
MINIMUM_DESCRIPTION_LENGTH = 10
# Number of worker threads (each one works in its own tab of the single Chrome window); the request rate grows with it
WORKER_COUNT = 5
# How many waiting documents the queue holds per worker (a small queue keeps the saved resume page accurate)
QUEUE_SLOTS_PER_WORKER = 2
# How often a waiting thread looks again at the queue and at the abort flag, in seconds
QUEUE_POLL_SECONDS = 0.5
# Chrome's debugging port only listens on this local address, so nothing outside this computer can connect
REMOTE_DEBUGGING_HOST = "127.0.0.1"
# How a PDF is downloaded: "fetch" (inside Chrome, with a timeout; recommended) or "click" (Chrome's own download event)
DOWNLOAD_METHOD = "fetch"
# A single PDF download may take this long in milliseconds before it counts as failed and is downloaded again
DOWNLOAD_TIMEOUT_MS = 600_000
# The PDF is moved from Chrome to Python in pieces of this many bytes (keeps memory use and message sizes small)
DOWNLOAD_CHUNK_BYTES = 3 * 1024 * 1024
# A PDF without the %%EOF end marker is downloaded again this many times (0 = never: the same URL is not requested twice)
INCOMPLETE_DOWNLOAD_REDOWNLOADS = 0
# How often a worker tries to connect to the shared Chrome before the session is restarted
WORKER_CONNECT_ATTEMPTS = 3

# ===========================================================================
# MESSAGES
# ===========================================================================
# Shown in the terminal only when the automatic log-in fails
START_INSTRUCTIONS = (
    "\n>> In the browser window:\n"
    "   1. Log in\n"
    "   2. Go to the search page and select Advanced Search\n"
    "   3. Click Search (leave the filters empty to get EVERYTHING)\n"
    "   4. Stay on results page 1 (finished documents are skipped, the run continues at the saved page)\n"
    "   Then press Enter here..."
)
# Shown when no tab shows a logout link
NOT_LOGGED_IN_REMINDER = "\n   You do not seem to be logged in yet. Log in, then press Enter here..."
# Shown when the results grid is missing
NO_RESULTS_REMINDER = (
    "\n   I cannot see the results grid. Select Advanced Search, click Search,\n"
    "   wait for the results to appear, then press Enter here..."
)

# ===========================================================================
# WEBSITE DETAILS
# ===========================================================================
# Base URL of the website
BASE_URL = "https://assist.dla.mil"
# Addresses to ping: the first on its own, the others (backup) only while the first gets no reply
PING_TARGETS = ("1.1.1.1", "8.8.8.8")
# A ping reply line contains "ttl=" (Linux, macOS and Windows)
PING_REPLY_PATTERN = re.compile(pattern=r"ttl=", flags=re.IGNORECASE)
# Log-in / start page
START_PAGE_URL = BASE_URL + "/online/start/"
# Page that is opened after the log-in to run the search
SEARCH_PAGE_URL = BASE_URL + "/AssistNet/Analyse/FilteredSearchA.aspx"
# A small read-only page the browser requests now and then (only when KEEP_ALIVE_ENABLED is True)
KEEP_ALIVE_PATH = "/online/doc_analysis/doc_info_general.cfm?ident_number=2"
# JavaScript that runs inside every page of the site: the browser itself pings, so it works while Python waits for a download
KEEP_ALIVE_JAVASCRIPT = (
    "(() => {"
    # Only the top page of the site, and only once per page
    "if (window.top !== window || location.hostname !== "
    + json.dumps(urlparse(url=BASE_URL).hostname)
    + " || window.__assistKeepAlive) { return; }"
    # Repeat forever
    "window.__assistKeepAlive = setInterval(() => {"
    # Request the small page with the log-in cookies and without using the cache
    "fetch("
    + json.dumps(KEEP_ALIVE_PATH)
    + ", {credentials: 'include', cache: 'no-store'})"
    # Report success or failure to the console hook
    ".then(response => console.log('[keep-alive] HTTP ' + response.status))"
    ".catch(error => console.log('[keep-alive] failed: ' + error));"
    # Interval length in milliseconds
    "}, " + str(KEEP_ALIVE_SECONDS * 1000) + ");"
    "})();"
)
# The password field of a log-in form
PASSWORD_FIELD_SELECTOR = "input[type='password']"
# A password field that Chrome has autofilled
PASSWORD_AUTOFILLED_SELECTOR = PASSWORD_FIELD_SELECTOR + ":-webkit-autofill"
# True when the log-in form is gone or a logout link is present
LOGIN_DONE_JAVASCRIPT = (
    "() => document.querySelector(\"input[type='password']\") === null"
    " || document.documentElement.innerHTML.toLowerCase().includes('logout')"
)
# Text of the "Search Method" control
SEARCH_METHOD_PATTERN = re.compile(pattern=r"search\s*method", flags=re.IGNORECASE)
# Text of the "Advanced Search" control
ADVANCED_SEARCH_PATTERN = re.compile(pattern=r"advanced\s*search", flags=re.IGNORECASE)
# Accessible name of the Search button
SEARCH_BUTTON_PATTERN = re.compile(pattern=r"^\s*search\s*$", flags=re.IGNORECASE)
# Backup way to find the Search button
SEARCH_BUTTON_FALLBACK_SELECTOR = (
    "input[type='submit'][value='Search' i], input[type='button'][value='Search' i], "
    "button:text-is('Search')"
)
# Document page URL (the {} is replaced by the internal document ID)
DOCUMENT_PAGE_URL_TEMPLATE = BASE_URL + "/online/doc_analysis/doc_info_general.cfm?ident_number={}"
# Folder where PDFs are saved
PDF_FOLDER = Path("PDFs")
# Folder for script data
OUTPUT_FOLDER = Path("assist_data")
# Persistent browser profile (keeps you logged in)
BROWSER_PROFILE_FOLDER = OUTPUT_FOLDER / "browser_profile"
# Local database: every ID and every file seen so far
STATE_DATABASE_FILE = OUTPUT_FOLDER / "state.sqlite3"
# Creates the tables on first use. Column names are unchanged from earlier versions, so existing databases keep working
DATABASE_SCHEMA = (
    # IDs seen on the website
    "CREATE TABLE IF NOT EXISTS documents ("
    "ident TEXT PRIMARY KEY, doc_id TEXT NOT NULL, first_seen TEXT NOT NULL, "
    "last_checked TEXT NOT NULL, pdf_count INTEGER NOT NULL);"
    # PDFs saved
    "CREATE TABLE IF NOT EXISTS files ("
    "ident TEXT NOT NULL, dmxid TEXT NOT NULL, url TEXT NOT NULL, path TEXT NOT NULL, "
    "bytes INTEGER NOT NULL, sha256 TEXT NOT NULL, suspect INTEGER NOT NULL, saved_at TEXT NOT NULL, "
    "PRIMARY KEY (ident, dmxid));"
    # Files the server refused to send
    "CREATE TABLE IF NOT EXISTS refused ("
    "ident TEXT NOT NULL, dmxid TEXT NOT NULL, url TEXT NOT NULL, refused_at TEXT NOT NULL, reason TEXT NOT NULL, "
    "PRIMARY KEY (ident, dmxid));"
    # The results page where the last run stopped
    "CREATE TABLE IF NOT EXISTS resume ("
    "name TEXT PRIMARY KEY, page_number INTEGER NOT NULL, total_pages INTEGER NOT NULL, updated_at TEXT NOT NULL);"
    # Incomplete-looking files that were saved (kept for compatibility with older databases)
    "CREATE TABLE IF NOT EXISTS suspect_checks ("
    "ident TEXT NOT NULL, dmxid TEXT NOT NULL, checked_at TEXT NOT NULL, "
    "PRIMARY KEY (ident, dmxid));"
)
# Extension of downloaded files
PDF_FILE_EXTENSION = ".pdf"
# Extension of a file that is still being written
PARTIAL_FILE_EXTENSION = ".part"
# Inserted before ".pdf" for files that still lack the end marker
SUSPECT_MARKER = ".suspect"

# ===========================================================================
# FILE NAME RULES
# ===========================================================================
# Names that Windows refuses for files and folders
RESERVED_WINDOWS_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)
# Maximum length of a folder name
FOLDER_NAME_MAXIMUM_LENGTH = 60
# Maximum length of the description part of a file name
DESCRIPTION_MAXIMUM_LENGTH = 50
# Maximum length of the file number part of a file name
FILE_NUMBER_MAXIMUM_LENGTH = 20
# Folder name prefix if the public document ID is missing
FALLBACK_FOLDER_PREFIX = "ident_"
# Description used if it is missing
FALLBACK_DESCRIPTION = "document"
# Date text used if it is missing or unparseable
FALLBACK_DATE_TEXT = "no-date"
# File number used if it is missing
FALLBACK_FILE_NUMBER = "no-number"
# Date layouts tried in turn when reading dates from the website
DATE_INPUT_FORMATS = ("%d-%b-%Y", "%d-%B-%Y", "%m/%d/%Y", "%Y-%m-%d")
# Date layout used in file names
DATE_OUTPUT_FORMAT = "%Y-%m-%d"

# ===========================================================================
# PAGE ELEMENTS, JAVASCRIPT AND PATTERNS
# ===========================================================================
# Search results grid
RESULTS_GRID_SELECTOR = "#GV"
# Page counter label ("Page X of Y")
PAGE_LABEL_SELECTOR = "#lblPageCount"
# Next page button
NEXT_PAGE_BUTTON_SELECTOR = "#btnNext"
# How long a jump to a typed page number may take before it counts as failed (Next-button clicking is used instead)
PAGE_JUMP_TIMEOUT_MS = 30_000
# Where the "go to page" box may be, tried in this order (put the real selector FIRST if the box is not found)
PAGE_NUMBER_BOX_SELECTORS = (
    # The real page-number box of the results page (Enter in it clicks "Go to Page")
    "#txtPgNum",
    # Common ASP.NET names
    "#txtPage",
    "#txtGoToPage",
    "#txtPageNo",
    "#txtPageNumber",
    # Any visible input whose id contains "page"
    "input[id*='page' i]:not([type='hidden']):not([type='button']):not([type='submit']):not([type='image'])",
    # Any visible input whose name contains "page"
    "input[name*='page' i]:not([type='hidden']):not([type='button']):not([type='submit']):not([type='image'])",
)
# Extracts the HTML of an element
GET_OUTER_HTML_JAVASCRIPT = "element => element.outerHTML"
# Returns the internal ID of the first document in the grid ('' if none)
FIRST_INTERNAL_ID_JAVASCRIPT = (
    "() => {"
    # First document link of the grid
    "const link = document.querySelector('#GV a[href*=\"fireMe\"]');"
    # No rows at all
    "if (link === null) { return ''; }"
    # Extract the ID from the link
    "const found = /fireMe\\('(\\d+)'\\)/.exec(link.getAttribute('href') || '');"
    "return found ? found[1] : '';"
    "}"
)
# True when the label shows the wanted page AND the grid was really replaced
PAGE_CHANGE_CHECK_JAVASCRIPT = (
    # Takes {pageNumber, previousInternalId}
    "(expected) => {"
    "const pageLabel = document.getElementById('lblPageCount');"
    # No label yet
    "if (pageLabel === null) { return false; }"
    "const labelText = pageLabel.textContent.replace(/\\u00a0/g, ' ').trim();"
    # The label shows another page
    "if (!labelText.startsWith('Page ' + expected.pageNumber + ' ')) { return false; }"
    # Nothing to compare with
    "if (expected.previousInternalId === '') { return true; }"
    "const link = document.querySelector('#GV a[href*=\"fireMe\"]');"
    "const found = link === null ? null : /fireMe\\('(\\d+)'\\)/.exec(link.getAttribute('href') || '');"
    # Empty while the grid is redrawn
    "const currentInternalId = found ? found[1] : '';"
    # The grid must differ from before
    "return currentInternalId !== '' && currentInternalId !== expected.previousInternalId;"
    "}"
)
# Clicks the real PDF link and forces Chrome to download instead of display
CLICK_DOWNLOAD_JAVASCRIPT = (
    "(url) => {"
    # The real link on the page, if present
    "const existing = Array.from(document.querySelectorAll('a[href]')).find(link => link.href === url);"
    # Otherwise a temporary link
    "const link = existing || document.createElement('a');"
    "if (!existing) { link.href = url; document.body.appendChild(link); }"
    # Download instead of display
    "link.setAttribute('download', '');"
    # Click it like a person would
    "link.click();"
    # Remove the temporary link again
    "if (!existing) { link.remove(); }"
    "}"
)
# Downloads one file inside Chrome and keeps its bytes in the page; the timer guarantees that the call always ends
FETCH_PDF_JAVASCRIPT = (
    # Takes {url, timeoutMs}
    "async (request) => {"
    # An abort controller stops a stalled download after the timeout
    "const controller = new AbortController();"
    "const timer = setTimeout(() => controller.abort(), request.timeoutMs);"
    "try {"
    # Same-origin request with the log-in cookies, never from the cache
    "const response = await fetch(request.url, {credentials: 'include', cache: 'no-store', signal: controller.signal});"
    # Read the whole body (the abort also stops a stalled body)
    "const bytes = new Uint8Array(await response.arrayBuffer());"
    # Keep the bytes in the page until Python has read them
    "window.__assistPdfBytes = bytes;"
    # Report what Python needs to judge the download
    "return {status: response.status, statusText: response.statusText, "
    "contentType: response.headers.get('content-type') || '', "
    "contentLength: response.headers.get('content-length') || '', "
    "contentEncoding: response.headers.get('content-encoding') || '', "
    "retryAfter: response.headers.get('retry-after') || '', size: bytes.length};"
    "} finally { clearTimeout(timer); }"
    "}"
)
# Returns one piece of the downloaded bytes as base64 text (null when the page lost the bytes)
READ_PDF_CHUNK_JAVASCRIPT = (
    # Takes [offset, length]
    "([offset, length]) => {"
    "const bytes = window.__assistPdfBytes;"
    "if (!bytes) { return null; }"
    "const slice = bytes.subarray(offset, offset + length);"
    "let binary = '';"
    # Convert in small steps, because a huge argument list would overflow the call stack
    "for (let position = 0; position < slice.length; position += 32768) {"
    "binary += String.fromCharCode.apply(null, slice.subarray(position, position + 32768));"
    "}"
    "return btoa(binary);"
    "}"
)
# Frees the memory of the downloaded bytes inside the page
DISCARD_PDF_JAVASCRIPT = "() => { delete window.__assistPdfBytes; }"
# All rows of the results grid
GRID_ROW_XPATH = "//table[@id='GV']//tr"
# The fireMe link of a row
GRID_LINK_XPATH = "string(.//a[contains(@href,'fireMe')]/@href)"
# Standard cells of a row
ROW_CELLS_XPATH = "./td"
# Identifies a PDF download link
PDF_LINK_MARKER = "loguser_hash.cfm"
# All PDF links of a page
PDF_LINK_XPATH = "//a[contains(@href,'" + PDF_LINK_MARKER + "')]"
# The table row that contains a link
PARENT_ROW_XPATH = "ancestor::tr[1]"
# All cells (data and header) of a row
PARENT_ROW_CELLS_XPATH = "./td|./th"
# URL parameter that holds the file number
FILE_NUMBER_QUERY_NAME = "dmxid"
# Column of the public document ID in the grid
GRID_COLUMN_PUBLIC_ID = 1
# Column of the description on the document page
DOCUMENT_COLUMN_DESCRIPTION = 1
# Column of the date on the document page
DOCUMENT_COLUMN_DATE = 3
# Extracts the internal ID from a JavaScript call
OPEN_DOCUMENT_CALL_PATTERN = re.compile(pattern=r"fireMe\('(?P<internal_id>\d+)'\)")
# Extracts the page numbers from "Page X of Y"
PAGE_LABEL_PATTERN = re.compile(pattern=r"Page\s+(?P<current>\d+)\s+of\s+(?P<total>\d+)")
# Only logged-in pages contain this
LOGOUT_LINK_MARKER = "logout.cfm"
# Indicates a login page
LOGIN_WORD_MARKER = b"login"
# Start of every PDF
PDF_SIGNATURE = b"%PDF-"
# End of every complete PDF
PDF_END_MARKER = b"%%EOF"
# Non-breaking space character used in HTML
NON_BREAKING_SPACE = "\xa0"
# Bytes read when looking for the PDF signature
PDF_SIGNATURE_SEARCH_BYTES = 1024
# Bytes read at the end of a file when looking for the end marker (some PDFs have a long trailer)
PDF_END_SEARCH_BYTES = 8192
# Bytes read when looking for a login page
LOGIN_PAGE_SEARCH_BYTES = 5000
# Header that tells how long to wait
RETRY_AFTER_HEADER_NAME = "retry-after"
# Usually temporary HTTP status codes
HTTP_RETRYABLE_STATUSES = {408, 425, 429, 500, 502, 503, 504}


# ===========================================================================
# SHARED STATE
# ===========================================================================
# One object holds every running counter and every piece of shared state
@dataclass(kw_only=True)
class RunState:
    # --- Counters shown in the summaries ---
    # Documents processed (retries are not counted again)
    documents_processed: int = 0
    # Documents skipped because an earlier run finished them (no request is made for them)
    documents_skipped: int = 0
    # PDFs saved
    pdfs_saved: int = 0
    # PDFs skipped because they already exist
    pdfs_skipped: int = 0
    # Problems (documents or files that could not be downloaded)
    problem_count: int = 0
    # Saved PDFs that still look incomplete
    suspect_pdf_count: int = 0
    # Documents without any PDF
    documents_without_pdfs: int = 0
    # Total PDF bytes written
    bytes_written: int = 0
    # Documents that failed first but worked later
    documents_recovered: int = 0
    # Browser session restarts
    browser_restarts: int = 0
    # --- Progress and restart control ---
    # Restarts since the last NEW results page was reached (grows the restart wait)
    restarts_without_progress: int = 0
    # Paging/reading failures in a row since the last NEW results page was reached
    paging_failures_without_progress: int = 0
    # The highest results page reached in this run
    furthest_results_page: int = 0
    # True after a redirect loop: the next Chrome start deletes the site cookies
    clear_cookies_on_next_start: bool = False
    # True after a page-number jump failed: the rest of the run clicks Next instead
    page_jump_unavailable: bool = False
    # How often a request label met a "not logged in" answer (a second time means: logging in again did not help)
    session_expired_counts: dict[str, int] = field(default_factory=dict)
    # --- Browser and network health ---
    # True when Chrome died or was closed (then the session restarts)
    browser_is_closed: bool = False
    # The browser context of the running session (late events of old contexts are ignored)
    active_browser_context: Any = None
    # True while the watchdog may stop Chrome because ping gets no reply
    network_watch_enabled: bool = False
    # True while the internet is down (then Chrome is stopped and restarted)
    network_is_down: bool = False
    # True when the run stopped because paging or reading a results page failed for good
    stopped_early: bool = False
    # --- Circuit breaker ---
    # Failed documents in a row
    consecutive_document_failures: int = 0
    # Breaker trips in a row (grows the pause)
    circuit_breaker_trips: int = 0
    # --- Shared resources ---
    # The local TCP port on which this session's Chrome accepts the worker threads' connections
    remote_debugging_port: int = 0
    # The local database (opened on first use)
    database_connection: sqlite3.Connection | None = None
    # True when the database was empty at the start (then nothing is announced as new)
    is_first_run: bool = False
    # Moment the script started (monotonic clock)
    started_at_monotonic: float = field(default_factory=time.monotonic)
    # --- Collections for the final summaries ---
    # Every recorded problem
    recorded_problems: list[ProblemRecord] = field(default_factory=list)
    # Paths of files saved as suspect
    suspect_file_paths: list[str] = field(default_factory=list)
    # Files the server refused, written as 'internal ID/file number'
    refused_file_labels: list[str] = field(default_factory=list)
    # Documents that still fail: internal ID -> failing document
    failing_documents: dict[str, FailingDocument] = field(default_factory=dict)
    # IDs not seen in earlier runs: internal ID -> public ID
    new_documents: dict[str, str] = field(default_factory=dict)
    # Known IDs that got new files: internal ID -> file numbers
    updated_documents: dict[str, list[str]] = field(default_factory=dict)


# The adaptive delay between requests (grows when the server complains, shrinks on success)
@dataclass(kw_only=True)
class AdaptiveDelay:
    # The normal delay and the lower limit
    base_seconds: float
    # The upper limit
    maximum_seconds: float
    # The delay in effect right now (set to the base delay in __post_init__)
    current_seconds: float = 0.0
    # Protects the delay because several worker threads change it
    lock: threading.Lock = field(default_factory=threading.Lock)

    # Start with the normal delay
    def __post_init__(self) -> None:
        self.current_seconds = self.base_seconds

    # Double the delay because the server complained (safe to call from several threads)
    def increase(self) -> None:
        # Only one thread at a time may change the delay
        with self.lock:
            # Remember the old value for the log
            old_seconds = self.current_seconds
            # Double it but respect the maximum
            self.current_seconds = min(self.maximum_seconds, old_seconds * 2)
            # Remember the new value for the log
            new_seconds = self.current_seconds
        # Only report real changes (at the maximum nothing changes any more)
        if new_seconds != old_seconds:
            logger.debug("Delay increased: %.2fs -> %.2fs", old_seconds, new_seconds)

    # Move the delay back towards normal after a success (safe to call from several threads)
    def decrease(self) -> None:
        # Only one thread at a time may change the delay
        with self.lock:
            # Remember the old value for the log
            old_seconds = self.current_seconds
            # Shrink it by 20 percent but never below the normal delay
            self.current_seconds = max(self.base_seconds, old_seconds * 0.8)
            # Remember the new value for the log
            new_seconds = self.current_seconds
        # Only report real changes
        if new_seconds != old_seconds:
            logger.debug("Delay decreased: %.2fs -> %.2fs", old_seconds, new_seconds)


# State of the background ping processes and the time of the last reply
@dataclass(kw_only=True)
class PingMonitor:
    # The running ping process of each address
    processes: dict[str, subprocess.Popen] = field(default_factory=dict)
    # When each ping process was started (monotonic clock)
    spawned_at: dict[str, float] = field(default_factory=dict)
    # Moment of the last reply of each address (monotonic clock)
    reply_times: dict[str, float] = field(default_factory=dict)
    # Moment the pings were last (re)started (monotonic clock)
    started_at: float = 0.0
    # Protects the processes while they are started or stopped
    lock: threading.Lock = field(default_factory=threading.Lock)
    # Held while the watchdog stops Chrome and ping, so the main thread waits for it
    outage_lock: threading.Lock = field(default_factory=threading.Lock)


# The one shared state object
run_state = RunState()
# The one background ping state object
ping_monitor = PingMonitor()
# The one adaptive delay
request_delay = AdaptiveDelay(base_seconds=SECONDS_BETWEEN_REQUESTS, maximum_seconds=MAXIMUM_DELAY_SECONDS)
# The named logger (its handlers are added in setup_logging)
logger = logging.getLogger(name="assist")
# Every line typed in the terminal
keyboard_lines: queue.Queue[str] = queue.Queue()
# State of the keyboard thread
keyboard_reader_state = {"started": False, "closed": False}
# Set at the end of the run to stop the network watchdog
watchdog_stop_event = threading.Event()
# Protects the counters of run_state that several threads change
state_lock = threading.RLock()
# Protects the one shared SQLite connection
database_lock = threading.RLock()
# Held by the worker that pauses for the circuit breaker, so the other workers wait for the pause too
circuit_breaker_lock = threading.Lock()
# Per-thread storage (every thread has its own download tab)
thread_local = threading.local()
# Set when any thread hit a problem that ends the current browser session (all threads then stop)
session_abort_event = threading.Event()
# The errors that ended the session (only the first one is kept and re-raised by the main thread)
session_abort_errors: list[BaseException] = []
# Set when the main thread asks the workers to stop normally (errors during that shutdown are then ignored)
worker_shutdown_event = threading.Event()


# ===========================================================================
# ERRORS RAISED ON PURPOSE
# ===========================================================================
# We are not (or no longer) logged in
class SessionExpiredError(Exception):
    pass


# HTTP 403: this one page or file is off limits
class ForbiddenError(Exception):
    pass


# A temporary problem that is worth retrying
class RetryableFetchError(Exception):
    # Stores the message plus the wait time the server asked for (if any)
    def __init__(self, message: str, wait_seconds: int | None = None) -> None:
        # Let Exception store the message
        super().__init__(message)
        # How long the server asked us to wait (or None)
        self.wait_seconds = wait_seconds


# A problem that retrying will not fix
class PermanentFetchError(Exception):
    pass


# The server refused to send a file (Chrome only reports 'canceled')
class RefusedDownloadError(PermanentFetchError):
    pass


# The browser tab or window is gone, or the session ended (Chrome is restarted)
class BrowserLostError(Exception):
    pass


# The internet connection was lost (Chrome is stopped and restarted later)
class NetworkDownError(BrowserLostError):
    pass


# Paging to or reading a results page failed for good (Chrome is restarted a few times, then the run stops)
class PagingFailedError(Exception):
    pass


# ===========================================================================
# SMALL REUSABLE HELPERS
# ===========================================================================
# Add to one numeric counter of run_state without losing updates from other threads
def add_to_counter(*, counter_name: str, amount: int = 1) -> None:
    # Only one thread at a time may change the counters
    with state_lock:
        # Read the counter, add the amount and store the result
        setattr(run_state, counter_name, getattr(run_state, counter_name) + amount)


# Sleep for a number of seconds and log pauses that are long enough to matter
def sleep_and_log(*, seconds: float) -> None:
    # Only report pauses worth mentioning
    if seconds >= LOG_SLEEPS_FROM_SECONDS:
        # Name of the function that is sleeping (one frame up the call stack)
        caller_name = sys._getframe(1).f_code.co_name
        # Report the pause
        logger.debug("Sleeping %.1fs (called from %s)", seconds, caller_name)
    # Really sleep
    time.sleep(seconds)


# Sleep for a number of seconds, but wake up at once when the browser session is aborted (used by the workers)
def sleep_unless_session_aborted(*, seconds: float) -> None:
    # Only report pauses worth mentioning
    if seconds >= LOG_SLEEPS_FROM_SECONDS:
        # Name of the function that is sleeping (one frame up the call stack)
        caller_name = sys._getframe(1).f_code.co_name
        # Report the pause
        logger.debug("Sleeping %.1fs unless the session is aborted (called from %s)", seconds, caller_name)
    # Returns early as soon as the abort flag is set
    session_abort_event.wait(timeout=seconds)


# Current local time as ISO text without fractions (used in the database)
def current_timestamp_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


# Turn seconds into h:mm:ss text
def format_elapsed_time(*, total_seconds: float) -> str:
    # Split off whole hours
    hours, remainder = divmod(int(total_seconds), 3600)
    # Split the rest into minutes and seconds
    minutes, seconds = divmod(remainder, 60)
    # Build the text with zero-padded minutes and seconds
    return f"{hours}:{minutes:02d}:{seconds:02d}"


# Delete a file and never complain
def delete_file_quietly(*, file_path: Path) -> None:
    # Deleting can fail
    try:
        # Remove the file if it exists
        file_path.unlink(missing_ok=True)
    # Permissions, file in use, ...
    except OSError:
        # Nothing more to do
        pass


# Wait the current adaptive delay with random jitter
def polite_pause() -> None:
    # Delay multiplied by a random factor, so requests do not look machine-regular
    pause_seconds = request_delay.current_seconds * random.uniform(a=JITTER_MINIMUM_FACTOR, b=JITTER_MAXIMUM_FACTOR)
    # Report the pause (always, even when short)
    logger.debug("Polite pause %.2fs", pause_seconds)
    # Sleep that long, but wake up at once when the browser session is aborted
    session_abort_event.wait(timeout=pause_seconds)


# Called by Playwright when the browser or its window is gone
def handle_browser_context_closed(closed_context: Any = None) -> None:
    # Ignore late close events of an OLD context: they would fake a crash of the new session
    if closed_context is None or closed_context is run_state.active_browser_context:
        # Report it
        logger.warning("Browser context closed (marking the browser as closed)")
        # Remember it, so every loop can stop quickly
        run_state.browser_is_closed = True


# Stop the current work when Chrome is gone or the internet is down
def raise_if_browser_unusable() -> None:
    # The network watchdog found no internet
    if run_state.network_is_down:
        # The main loop stops Chrome and waits for the internet
        raise NetworkDownError("the network went down")
    # Chrome died or was closed
    if run_state.browser_is_closed:
        # The main loop restarts the session
        raise BrowserLostError("the browser was closed or crashed")
    # Another thread already ended the session, so this thread stops its work too
    if session_abort_event.is_set():
        # The main thread re-raises the ORIGINAL error once all threads have stopped
        raise BrowserLostError("another thread ended the browser session")


# Does this error text mean that Chrome is unusable?
def error_means_browser_is_unusable(*, error: BaseException) -> bool:
    # Compare in lower case
    error_text = str(error).lower()
    # True if any known marker appears in the text
    return any(marker in error_text for marker in FATAL_BROWSER_ERROR_MARKERS)


# Call inside every except block: restarts Chrome when the error is fatal, otherwise returns
def restart_browser_if_error_is_fatal(error: BaseException) -> None:
    # A redirect loop never fixes itself: the session cookies are invalid
    if REDIRECT_LOOP_MARKER in str(error).lower():
        # Tell the user
        logger.warning(f"Redirect loop (the session is no longer valid): {error}")
        # The next Chrome start deletes the cookies and logs in again
        run_state.clear_cookies_on_next_start = True
        # Close Chrome and start again
        raise BrowserLostError("redirect loop (ERR_TOO_MANY_REDIRECTS): closing Chrome and logging in again")
    # Chrome is closed, crashed or disconnected
    if error_means_browser_is_unusable(error=error):
        # Tell the user
        logger.warning(f"Fatal browser error: {error}")
        # Remember it for every other loop
        run_state.browser_is_closed = True
    # Raises BrowserLostError (or NetworkDownError) when Chrome must restart
    raise_if_browser_unusable()


# Log everything one tab does
def attach_tab_logging(tab: Page) -> None:
    # Browser console output
    tab.on("console", lambda message: logger.debug("[console] %s: %s", message.type, message.text))
    # JavaScript errors on the page
    tab.on("pageerror", lambda error: logger.warning("[page error] %s", error))
    # Every navigation of every frame
    tab.on("framenavigated", lambda frame: logger.debug("[navigated] %s", frame.url))
    # HTML parsed
    tab.on("domcontentloaded", lambda loaded_tab: logger.debug("[domcontentloaded] %s", loaded_tab.url))
    # Page fully loaded
    tab.on("load", lambda loaded_tab: logger.debug("[load] %s", loaded_tab.url))
    # A new window was opened by the page
    tab.on("popup", lambda popup: logger.debug("[popup] %s", popup.url))
    # A tab was closed
    tab.on("close", lambda closed_tab: logger.debug("[tab closed] %s", closed_tab.url))
    # A download began
    tab.on(
        "download",
        lambda download: logger.info("[download started] %s -> %s", download.url, download.suggested_filename),
    )


# Log all network traffic of the browser
def attach_network_logging(browser_context: BrowserContext) -> None:
    # Every request sent
    browser_context.on(
        "request",
        lambda request: logger.debug("[request] %s %s (%s)", request.method, request.url, request.resource_type),
    )
    # Every response received
    browser_context.on("response", lambda response: logger.debug("[response] %s %s", response.status, response.url))
    # Every failed request
    browser_context.on(
        "requestfailed",
        lambda request: logger.warning("[request failed] %s %s: %s", request.method, request.url, request.failure),
    )
    # Every completed request
    browser_context.on("requestfinished", lambda request: logger.debug("[request finished] %s", request.url))


# Mark the browser as closed when a tab crashes (the context close event does not fire for a crashed tab)
def attach_tab_crash_handler(tab: Page) -> None:
    # Called by Playwright when the tab crashes
    def on_tab_crash(_crashed_tab: Page) -> None:
        # Tell the user
        logger.warning("A Chrome tab crashed")
        # Every loop stops and the session restarts
        run_state.browser_is_closed = True

    # Register the crash handler
    tab.on(event="crash", f=on_tab_crash)


# Log everything a tab does and make a crash of it restart Chrome
def watch_tab_for_crash(tab: Page) -> None:
    # Start logging this tab
    attach_tab_logging(tab)
    # Watch for a crash of this tab
    attach_tab_crash_handler(tab)


# A results page was reached: reset the restart counters only when it is a NEW furthest page
def record_results_page_progress(*, page_number: int) -> None:
    # Real progress (not the same page again after a restart)
    if page_number > run_state.furthest_results_page:
        # Remember the new furthest page
        run_state.furthest_results_page = page_number
        # The next restart waits only a short time again
        run_state.restarts_without_progress = 0
        # Paging failures start counting from zero again
        run_state.paging_failures_without_progress = 0


# ===========================================================================
# LOGGING, ALERTS AND START-UP CHECKS
# ===========================================================================
# Send all log lines to the screen
def setup_logging() -> None:
    # Some stdout objects can change their error handling
    if hasattr(sys.stdout, "reconfigure"):
        # Reconfiguring can fail
        try:
            # Never crash on characters the console cannot show
            sys.stdout.reconfigure(errors="replace")
        # The console refuses the change
        except (OSError, ValueError):
            # Carry on with the default behaviour
            pass
    # The log line layout
    log_formatter = logging.Formatter(fmt=LOG_LINE_FORMAT, datefmt=LOG_DATE_FORMAT)
    # The logger lets everything from LOG_LEVEL up through
    logger.setLevel(level=LOG_LEVEL)
    # Stop the root logger from printing every line a second time
    logger.propagate = False
    # Remove old handlers so lines are never printed twice
    logger.handlers.clear()
    # The handler that writes to the terminal
    screen_handler = logging.StreamHandler(stream=sys.stdout)
    # The handler also lets everything from LOG_LEVEL up through
    screen_handler.setLevel(LOG_LEVEL)
    # Apply the layout
    screen_handler.setFormatter(fmt=log_formatter)
    # Attach the handler
    logger.addHandler(hdlr=screen_handler)
    # Only in debug mode
    if LOG_LEVEL <= logging.DEBUG:
        # Playwright prints every API call (goto, click, evaluate, ...) to stderr
        os.environ.setdefault("DEBUG", "pw:api")


# Draw attention to a waiting prompt with the terminal bell
def ring_terminal_bell() -> None:
    print("\a", end="", flush=True)


# Is the ping command installed?
def ping_command_exists() -> bool:
    return shutil.which("ping") is not None


# The command that pings one address until it is stopped
def build_ping_command(*, target: str) -> list[str]:
    # Windows pings forever with -t
    if sys.platform == "win32":
        return ["ping", "-t", target]
    # Linux and macOS ping forever by default (-n: no name lookups)
    return ["ping", "-n", target]


# Read the output of one ping process and note every reply (runs in its own thread)
def read_ping_replies(*, process: Any, target: str) -> None:
    # The output stream can be closed at any moment
    try:
        # Every line ping prints
        for output_line in process.stdout:
            # Show every raw line
            logger.debug("[ping %s] %s", target, output_line.rstrip())
            # A reply line (not "unreachable" or "timeout")
            if PING_REPLY_PATTERN.search(output_line):
                # Remember when it arrived
                ping_monitor.reply_times[target] = time.monotonic()
    # The stream was closed because ping was stopped
    except (OSError, ValueError):
        # Nothing more to read
        return


# Start the ping process of one address (the caller holds ping_monitor.lock)
def start_ping_process(*, target: str) -> None:
    # Starting a process can fail
    try:
        # Run ping in the background and read its output line by line
        process = subprocess.Popen(
            build_ping_command(target=target),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            bufsize=1,
        )
    # The ping command cannot be started
    except OSError as error:
        # Explain
        logger.warning(f"Could not start ping for {target}: {error}")
        # Nothing more to do
        return
    # Report the start
    logger.debug("Started ping process for %s (pid %s)", target, process.pid)
    # Remember the process
    ping_monitor.processes[target] = process
    # Remember when it was started
    ping_monitor.spawned_at[target] = time.monotonic()
    # Read its output in the background (daemon: never blocks the exit)
    threading.Thread(
        target=read_ping_replies,
        kwargs={"process": process, "target": target},
        name=f"ping-{target}",
        daemon=True,
    ).start()


# Make sure the ping of one address runs (the caller holds ping_monitor.lock)
def restart_ping_if_stopped(*, target: str) -> None:
    # Its current process, if any
    process = ping_monitor.processes.get(target)
    # It is still running
    if process is not None and process.poll() is None:
        return
    # It was started a moment ago (ping quits at once when the network is unreachable): do not restart again and again
    if time.monotonic() - ping_monitor.spawned_at.get(target, float("-inf")) < PING_RESTART_MIN_SECONDS:
        return
    # Start it again
    start_ping_process(target=target)


# Stop the ping of one address (the caller holds ping_monitor.lock)
def stop_ping_process(*, target: str) -> None:
    # Forget its process
    process = ping_monitor.processes.pop(target, None)
    # Forget its start time
    ping_monitor.spawned_at.pop(target, None)
    # It was not running
    if process is None:
        return
    # Report the stop
    logger.debug("Stopping ping process for %s", target)
    # Stopping can fail
    try:
        # Stop it
        process.kill()
        # Wait until it is gone
        process.wait(timeout=2)
    # Already gone or stuck
    except (OSError, subprocess.TimeoutExpired):
        # Nothing more to do
        pass


# Moment of the newest reply of any address (0 if none yet)
def newest_ping_reply_time() -> float:
    return max(list(ping_monitor.reply_times.values()), default=0.0)


# Ping the first address; ping the backup address only while the first one stays silent
def maintain_ping_processes() -> None:
    # One thread at a time
    with ping_monitor.lock:
        # The address that is always tried first
        primary_target = PING_TARGETS[0]
        # Keep its ping running (restart it if it quit)
        restart_ping_if_stopped(target=primary_target)
        # How long the first address has been silent
        primary_silent_seconds = time.monotonic() - max(
            ping_monitor.reply_times.get(primary_target, 0.0), ping_monitor.started_at
        )
        # Every backup address
        for backup_target in PING_TARGETS[1:]:
            # The first address does not answer
            if primary_silent_seconds >= PING_BACKUP_AFTER_SECONDS:
                # The backup is not in use yet
                if backup_target not in ping_monitor.processes:
                    # Tell the user
                    logger.info(f"No ping reply from {primary_target}, also trying {backup_target}")
                # Ping the backup address
                restart_ping_if_stopped(target=backup_target)
            # The first address answers again and the backup still runs
            elif backup_target in ping_monitor.processes:
                # Tell the user
                logger.info(f"{primary_target} replies again, {backup_target} is not needed")
                # Stop the backup ping
                stop_ping_process(target=backup_target)


# Stop every ping process
def stop_all_ping_processes() -> None:
    # One thread at a time
    with ping_monitor.lock:
        # Every running ping
        for target in list(ping_monitor.processes):
            # Stop it
            stop_ping_process(target=target)


# (Re)start the background ping from scratch
def restart_ping_monitoring() -> None:
    # Stop the old ping processes
    stop_all_ping_processes()
    # One thread at a time
    with ping_monitor.lock:
        # No reply seen yet
        ping_monitor.reply_times.clear()
        # The silence timer starts now
        ping_monitor.started_at = time.monotonic()
        # Start with the first address only
        start_ping_process(target=PING_TARGETS[0])


# How long has ping been silent?
def seconds_without_ping_reply() -> float:
    # The last reply, or the moment ping was started
    last_sign_of_life = max(newest_ping_reply_time(), ping_monitor.started_at)
    # Seconds since then
    return time.monotonic() - last_sign_of_life


# Wait for a ping reply after ping was (re)started; True if one came
def wait_for_first_ping_reply(*, seconds: float) -> bool:
    # Moment to stop waiting
    deadline = time.monotonic() + seconds
    # Report the wait
    logger.debug("Waiting up to %.0fs for a ping reply", seconds)
    # Until the time is up
    while time.monotonic() < deadline:
        # A reply arrived after the start
        if newest_ping_reply_time() > ping_monitor.started_at:
            return True
        # Restart a ping that quit, and try the backup address if the first stays silent
        maintain_ping_processes()
        # Look again in a moment
        time.sleep(0.5)
    # One last look
    return newest_ping_reply_time() > ping_monitor.started_at


# Pause (never stop) until ping gets a reply; Chrome only starts after that
def wait_until_internet_works() -> None:
    # Report the check
    logger.info("Checking the internet connection with ping")
    # Wait until the watchdog has finished stopping Chrome and ping
    with ping_monitor.outage_lock:
        # The watchdog leaves Chrome alone while we wait
        run_state.network_watch_enabled = False
    # Without ping the internet cannot be checked
    if not ping_command_exists():
        # Report it
        logger.warning("ping is not available, skipping the internet check")
        # Carry on without monitoring
        return
    # Until ping gets a reply
    while True:
        # (Re)start ping
        restart_ping_monitoring()
        # A reply came
        if wait_for_first_ping_reply(seconds=PING_FIRST_REPLY_WAIT_SECONDS):
            # Tell the user
            logger.info("Ping gets replies, the internet works")
            # Chrome may start
            return
        # Tell the user
        logger.warning(f"No ping reply. Waiting {PING_RETRY_WAIT_SECONDS}s, then ping is restarted")
        # Stop ping while we wait
        stop_all_ping_processes()
        # Wait, then restart ping
        sleep_and_log(seconds=PING_RETRY_WAIT_SECONDS)


# Kill the Chrome processes of this script (safe to call from the watchdog thread)
def kill_chrome_processes() -> None:
    # psutil is not installed
    if psutil is None:
        # Explain
        logger.warning("psutil is not installed, so Chrome cannot be stopped from outside (pip install psutil)")
        # Nothing more to do
        return
    # The profile folder that identifies our Chrome (normcase: Windows ignores upper/lower case)
    profile_path = os.path.normcase(os.path.abspath(BROWSER_PROFILE_FOLDER))
    # Every running process
    for process in psutil.process_iter(["cmdline"]):
        # The process can vanish while we look at it
        try:
            # Every command line argument of the process
            for argument in process.info["cmdline"] or []:
                # This Chrome uses our profile folder
                if (
                    argument.startswith("--user-data-dir=")
                    and os.path.normcase(os.path.abspath(argument.split("=", 1)[1])) == profile_path
                ):
                    # Report the kill
                    logger.debug("Killing Chrome process %s", process.pid)
                    # Stop it
                    process.kill()
                    # Next process
                    break
        # Gone already or not allowed
        except psutil.Error:
            # Next process
            continue


# Watch the ping replies in the background and stop Chrome when they stay away
def run_network_watchdog() -> None:
    # Look now and then until the run ends
    while not watchdog_stop_event.wait(timeout=PING_CHECK_SECONDS):
        # The main thread waits while we handle an outage
        with ping_monitor.outage_lock:
            # Chrome is not running (or ping is not available)
            if not run_state.network_watch_enabled:
                # Nothing to watch
                continue
            # Show the silence timer on every look
            logger.debug("Watchdog: %.0fs since the last ping reply", seconds_without_ping_reply())
            # Restart a ping that exited
            maintain_ping_processes()
            # Ping still gets replies
            if seconds_without_ping_reply() < PING_SILENCE_SECONDS:
                # Chrome keeps going
                continue
            # Handle each outage only once
            run_state.network_watch_enabled = False
            # Tell the main thread
            run_state.network_is_down = True
            # Tell the user
            logger.warning(
                f"No ping reply for {PING_SILENCE_SECONDS}s. Stopping Chrome and ping; "
                "they start again when ping gets replies"
            )
            # Stop ping
            stop_all_ping_processes()
            # Stop Chrome, so no call stays stuck on the dead connection
            kill_chrome_processes()


# Run the network watchdog in its own thread
def start_network_watchdog_thread() -> None:
    # The ping command is missing
    if not ping_command_exists():
        # Explain
        logger.warning("The ping command was not found, so the internet is not monitored")
    # Chrome cannot be stopped from outside
    if psutil is None:
        # Explain the limit
        logger.warning("psutil is not installed: Chrome cannot be stopped when the internet goes down (pip install psutil)")
    # Never leave ping processes behind
    atexit.register(stop_all_ping_processes)
    # Daemon: never blocks the exit
    threading.Thread(target=run_network_watchdog, name="network-watchdog", daemon=True).start()
    # Report the start
    logger.info("Network watchdog thread started")


# PAUSE (never stop) while the disk is too full
def wait_for_free_disk_space() -> None:
    # The folder must exist so its disk can be measured
    PDF_FOLDER.mkdir(parents=True, exist_ok=True)
    # Show the free space on every check
    logger.debug("Free disk space: %d MB", shutil.disk_usage(path=PDF_FOLDER).free // BYTES_PER_MEGABYTE)
    # Compare with the safety margin
    while (free_bytes := shutil.disk_usage(path=PDF_FOLDER).free) < MINIMUM_FREE_DISK_BYTES:
        # Tell the user
        logger.warning(
            f"Only {free_bytes // BYTES_PER_MEGABYTE} MB of disk space left. "
            f"Free some space; checking again in {DISK_WAIT_SECONDS}s"
        )
        # Wait, then measure again
        sleep_and_log(seconds=DISK_WAIT_SECONDS)


# Check the environment (warnings only, nothing here can stop the script)
def run_preflight_checks() -> None:
    # Report the step
    logger.info("Running the preflight checks")
    # The Enter prompts work best in a real terminal
    if not sys.stdin.isatty():
        # Warn only
        logger.warning("stdin is not a terminal; if input ends, the Enter prompts continue by themselves")
    # Test that we may write in the output folder
    try:
        # Make sure the folder exists
        OUTPUT_FOLDER.mkdir(parents=True, exist_ok=True)
        # A throw-away test file
        probe_file = OUTPUT_FOLDER / ".write_test"
        # Try to write to it
        probe_file.write_text(data="ok", encoding="utf-8")
        # Delete it again
        probe_file.unlink()
        # Report success
        logger.debug("Write test in %s passed", OUTPUT_FOLDER.resolve())
    # Permissions, read-only disk, ...
    except OSError as error:
        # Warn and carry on
        logger.warning(f"Cannot write to {OUTPUT_FOLDER.resolve()}: {error}")
    # Check the free disk space without being able to crash the start
    try:
        # Wait here if the disk is too full
        wait_for_free_disk_space()
    # The disk could not be measured
    except OSError as error:
        # Warn only
        logger.warning(f"Could not measure the free disk space: {error}")


# ===========================================================================
# LOCAL DATABASE (WHAT WAS SEEN AND SAVED IN EARLIER RUNS)
# ===========================================================================
# Returned by read_database_row when the database itself could not be used (different from "no row found")
DATABASE_UNAVAILABLE = object()


# Open the local database once and reuse it
def get_database_connection() -> sqlite3.Connection:
    # Not opened yet
    if run_state.database_connection is None:
        # Make sure the folder exists
        OUTPUT_FOLDER.mkdir(parents=True, exist_ok=True)
        # Open (or create) the database file
        # check_same_thread=False lets every thread use it (database_lock allows only one at a time); timeout waits for locks
        connection = sqlite3.connect(database=str(STATE_DATABASE_FILE), check_same_thread=False, timeout=30)
        # Show every SQL statement (cut at 300 characters)
        connection.set_trace_callback(lambda statement: logger.debug("SQL: %s", " ".join(statement.split())[:300]))
        # Create the tables if they are missing
        connection.executescript(DATABASE_SCHEMA)
        # Remember the connection
        run_state.database_connection = connection
        # Report it
        logger.debug("Opened the database %s", STATE_DATABASE_FILE)
    # Hand the connection back
    return run_state.database_connection


# Close the local database at the end of the run
def close_database_connection() -> None:
    # It was opened
    if run_state.database_connection is not None:
        # Close it
        run_state.database_connection.close()
        # Forget it
        run_state.database_connection = None


# Run one or more write statements in ONE transaction; a database error is logged and never stops the run
def write_to_database(*, statements: list[tuple[str, tuple]]) -> None:
    # The database can fail
    try:
        # Only one thread at a time may use the shared connection; one transaction (committed on success, rolled back on error)
        with database_lock, get_database_connection() as connection:
            # Every (sql, parameters) pair
            for sql_text, parameters in statements:
                # Run it
                connection.execute(sql_text, parameters)
    # The database cannot be used
    except sqlite3.Error as error:
        # Warn and carry on
        logger.warning(f"Could not write to {STATE_DATABASE_FILE}: {error}")


# Read one row; returns the row, None (no row) or DATABASE_UNAVAILABLE (database error)
def read_database_row(*, sql_text: str, parameters: tuple = ()) -> Any:
    # The database can fail
    try:
        # Only one thread at a time may use the shared connection
        with database_lock:
            # Run the query and take the first row
            return get_database_connection().execute(sql_text, parameters).fetchone()
    # The database cannot be used
    except sqlite3.Error as error:
        # Warn and carry on
        logger.warning(f"Could not query {STATE_DATABASE_FILE}: {error}")
        # Tell the caller that the answer is unknown
        return DATABASE_UNAVAILABLE


# Tell the user what earlier runs already know
def log_database_overview() -> None:
    # The database can fail
    try:
        # Open the database
        connection = get_database_connection()
        # IDs known
        document_count = connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        # Files known
        file_count = connection.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    # The database cannot be used
    except sqlite3.Error as error:
        # Warn and carry on
        logger.warning(f"Could not read {STATE_DATABASE_FILE}: {error}")
        # Nothing more to report
        return
    # An empty database means the very first run
    run_state.is_first_run = document_count == 0 and file_count == 0
    # What happens to the known documents depends on the setting
    known_documents_text = (
        "Every ID is opened again; only new files are requested."
        if RECHECK_KNOWN_DOCUMENTS
        else "Finished IDs are not opened again; only unfinished IDs and new IDs are requested."
    )
    # Tell the user
    logger.info(f"Earlier runs know {document_count} IDs and {file_count} files. {known_documents_text}")


# Was this ID seen in an earlier run?
def is_document_known(*, internal_id: str) -> bool:
    # Look it up in both tables
    row = read_database_row(
        sql_text="SELECT 1 FROM documents WHERE ident = ? UNION SELECT 1 FROM files WHERE ident = ? LIMIT 1",
        parameters=(internal_id, internal_id),
    )
    # Known if any row exists; an unusable database counts as unknown
    return row is not None and row is not DATABASE_UNAVAILABLE


# Was this ID completely finished (every file saved) in an earlier run? Then its page is not opened again
def was_document_checked_before(*, internal_id: str) -> bool:
    # Only record_document_checked writes this table, and only after every file of the document worked
    row = read_database_row(sql_text="SELECT 1 FROM documents WHERE ident = ?", parameters=(internal_id,))
    # An unusable database counts as "not finished", so nothing is skipped by mistake
    return row is not None and row is not DATABASE_UNAVAILABLE


# Where did an earlier run save this file (None if never)
def find_recorded_file_path(*, internal_id: str, file_number: str) -> Path | None:
    # Look it up
    row = read_database_row(
        sql_text="SELECT path FROM files WHERE ident = ? AND dmxid = ?", parameters=(internal_id, file_number)
    )
    # Nothing known
    if row is None or row is DATABASE_UNAVAILABLE:
        return None
    # The recorded path
    return Path(row[0])


# Did the server refuse this file within the last REFUSED_RECHECK_HOURS?
def was_file_refused_recently(*, internal_id: str, file_number: str) -> bool:
    # Look it up
    row = read_database_row(
        sql_text="SELECT refused_at FROM refused WHERE ident = ? AND dmxid = ?",
        parameters=(internal_id, file_number),
    )
    # Never refused (or unknown): ask for it
    if row is None or row is DATABASE_UNAVAILABLE:
        return False
    # Recent enough to leave it alone
    return datetime.now() - datetime.fromisoformat(row[0]) < timedelta(hours=REFUSED_RECHECK_HOURS)


# Remember that the server refused a file
def record_refused_file(*, document_record: DocumentRecord, pdf_link: PdfLink, reason: str) -> None:
    # Insert or replace the row of this file
    write_to_database(
        statements=[
            (
                "INSERT OR REPLACE INTO refused (ident, dmxid, url, refused_at, reason) VALUES (?, ?, ?, ?, ?)",
                (
                    document_record["internal_id"],
                    pdf_link["file_number"],
                    pdf_link["download_url"],
                    current_timestamp_iso(),
                    reason,
                ),
            )
        ]
    )


# Remember one saved PDF (and forget any earlier refusal of it)
def record_saved_file(
    *,
    document_record: DocumentRecord,
    pdf_link: PdfLink,
    saved_path: Path,
    downloaded_pdf: DownloadedPdf,
    is_suspect: bool,
) -> None:
    # Both statements run in one transaction
    write_to_database(
        statements=[
            (
                "INSERT OR REPLACE INTO files (ident, dmxid, url, path, bytes, sha256, suspect, saved_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    document_record["internal_id"],
                    pdf_link["file_number"],
                    pdf_link["download_url"],
                    str(saved_path),
                    downloaded_pdf.size_bytes,
                    downloaded_pdf.sha256_hex,
                    int(is_suspect),
                    current_timestamp_iso(),
                ),
            ),
            # A saved file is no longer refused
            (
                "DELETE FROM refused WHERE ident = ? AND dmxid = ?",
                (document_record["internal_id"], pdf_link["file_number"]),
            ),
        ]
    )


# Remember that an incomplete-looking file was saved (it is never downloaded a second time)
def record_suspect_retry(*, document_record: DocumentRecord, pdf_link: PdfLink) -> None:
    # Insert or replace the row of this file
    write_to_database(
        statements=[
            (
                "INSERT OR REPLACE INTO suspect_checks (ident, dmxid, checked_at) VALUES (?, ?, ?)",
                (document_record["internal_id"], pdf_link["file_number"], current_timestamp_iso()),
            )
        ]
    )


# Forget the suspect mark of a file that is complete now
def clear_suspect_retry(*, document_record: DocumentRecord, pdf_link: PdfLink) -> None:
    # Remove the row of this file
    write_to_database(
        statements=[
            (
                "DELETE FROM suspect_checks WHERE ident = ? AND dmxid = ?",
                (document_record["internal_id"], pdf_link["file_number"]),
            )
        ]
    )


# Which results page did the last run stop at? (None if unknown)
def load_saved_results_page() -> int | None:
    # Resuming is switched off
    if not RESUME_FROM_SAVED_PAGE:
        return None
    # Look up the saved page
    row = read_database_row(sql_text="SELECT page_number FROM resume WHERE name = ?", parameters=(RESUME_KEY,))
    # Nothing saved (or database unusable): start at the page the browser shows
    if row is None or row is DATABASE_UNAVAILABLE:
        return None
    # The saved page number
    return int(row[0])


# Remember the results page that is being worked on now
def save_current_results_page(*, page_number: int, total_pages: int) -> None:
    # Insert or replace the single resume row
    write_to_database(
        statements=[
            (
                "INSERT OR REPLACE INTO resume (name, page_number, total_pages, updated_at) VALUES (?, ?, ?, ?)",
                (RESUME_KEY, page_number, total_pages, current_timestamp_iso()),
            )
        ]
    )


# Forget the saved page (the whole search was finished)
def clear_saved_results_page() -> None:
    # Remove the resume row
    write_to_database(statements=[("DELETE FROM resume WHERE name = ?", (RESUME_KEY,))])


# Record a file that is on disk but unknown to the database (no request involved)
def record_file_found_on_disk(*, document_record: DocumentRecord, pdf_link: PdfLink, saved_path: Path) -> None:
    # Reading the file can fail
    try:
        # Inspect and hash the file on the local disk
        downloaded_pdf = inspect_downloaded_pdf(partial_path=saved_path)
    # Not readable or not a PDF
    except (OSError, PermanentFetchError, RetryableFetchError, SessionExpiredError):
        # Leave it alone
        return
    # Add it to the database
    record_saved_file(
        document_record=document_record,
        pdf_link=pdf_link,
        saved_path=saved_path,
        downloaded_pdf=downloaded_pdf,
        is_suspect=SUSPECT_MARKER in saved_path.name,
    )


# Remember that an ID was checked now (insert it, or update its last check)
def record_document_checked(*, document_record: DocumentRecord, pdf_count: int) -> None:
    # The time of this check
    now = current_timestamp_iso()
    # Upsert the row
    write_to_database(
        statements=[
            (
                "INSERT INTO documents (ident, doc_id, first_seen, last_checked, pdf_count) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(ident) DO UPDATE SET doc_id = excluded.doc_id, "
                "last_checked = excluded.last_checked, pdf_count = excluded.pdf_count",
                (document_record["internal_id"], document_record["public_id"], now, now, pdf_count),
            )
        ]
    )


# Announce an ID that no earlier run has seen
def announce_new_document(*, document_record: DocumentRecord) -> None:
    # Internal ID
    internal_id = document_record["internal_id"]
    # Check and remember in ONE step, so two workers can never announce the same ID twice
    with state_lock:
        # The first run knows nothing, and each ID is announced only once
        if run_state.is_first_run or internal_id in run_state.new_documents:
            return
        # Remember it for the final summary
        run_state.new_documents[internal_id] = document_record["public_id"]
    # Announce it (outside the lock, logging can be slow)
    logger.info(f"NEW ID {internal_id} ({document_record['public_id']})")


# Announce a new file on an ID that earlier runs already knew
def announce_new_file(*, document_record: DocumentRecord, pdf_link: PdfLink, document_is_new: bool) -> None:
    # New IDs were announced already
    if document_is_new:
        return
    # Internal ID
    internal_id = document_record["internal_id"]
    # The dictionary is shared by all workers
    with state_lock:
        # Remember it for the final summary
        run_state.updated_documents.setdefault(internal_id, []).append(pdf_link["file_number"])
    # Announce it
    logger.info(f"NEW FILE on known ID {internal_id} ({document_record['public_id']}): file {pdf_link['file_number']}")


# ===========================================================================
# PARSING THE WEB PAGES
# ===========================================================================
# Extract and clean the text of an HTML element
def extract_clean_text(*, html_element: Any) -> str:
    # Replace non-breaking spaces and collapse all whitespace
    return " ".join(html_element.text_content().replace(NON_BREAKING_SPACE, " ").split())


# Safely get the text of one cell (empty text when the row is shorter)
def get_cell_text_or_empty(*, cell_texts: list[str], column_index: int) -> str:
    return cell_texts[column_index] if column_index < len(cell_texts) else ""


# Turn bytes into text without ever failing
def decode_html_bytes(*, page_body: bytes) -> str:
    # Replace undecodable bytes
    return page_body.decode(encoding="utf-8", errors="replace")


# Parse the search results HTML into one record per document
def parse_results_grid(*, html_text: str) -> list[DocumentRecord]:
    # Parse the HTML string
    html_document = lxml_html.document_fromstring(html=html_text)
    # The records found
    document_records: list[DocumentRecord] = []
    # IDs already seen on this page (duplicate rows are skipped)
    seen_internal_ids: set[str] = set()
    # Every row of the results table
    for table_row in html_document.xpath(GRID_ROW_XPATH):
        # The document link of the row
        match_found = OPEN_DOCUMENT_CALL_PATTERN.search(string=table_row.xpath(GRID_LINK_XPATH) or "")
        # Header row, footer row, ...
        if not match_found:
            continue
        # The internal document ID
        internal_id = match_found.group("internal_id")
        # A duplicate row
        if internal_id in seen_internal_ids:
            continue
        # Remember this ID
        seen_internal_ids.add(internal_id)
        # Clean text of all cells
        cell_texts = [extract_clean_text(html_element=cell) for cell in table_row.xpath(ROW_CELLS_XPATH)]
        # Add the record
        document_records.append(
            {
                "internal_id": internal_id,
                "public_id": get_cell_text_or_empty(cell_texts=cell_texts, column_index=GRID_COLUMN_PUBLIC_ID),
            }
        )
    # Show every document found on the page
    logger.debug(
        "Results grid parsed: %d documents: %s",
        len(document_records),
        [record["public_id"] for record in document_records],
    )
    # Hand the records back
    return document_records


# Parse a document page into its PDF links
def parse_document_page(*, html_text: str, page_url: str) -> list[PdfLink]:
    # Parse the HTML string
    html_document = lxml_html.document_fromstring(html=html_text)
    # The links found
    pdf_links: list[PdfLink] = []
    # File numbers already processed (duplicates are skipped)
    seen_file_numbers: set[str] = set()
    # Every PDF link of the page
    for link_element in html_document.xpath(PDF_LINK_XPATH):
        # lxml already decoded entities, so the href is never decoded again
        absolute_url = urljoin(base=page_url, url=link_element.get(key="href", default=""))
        # The file number from the URL
        file_number = parse_qs(qs=urlparse(url=absolute_url).query).get(FILE_NUMBER_QUERY_NAME, [""])[0]
        # The link has no file number at all
        if not file_number:
            # Invent a unique one so file names never collide
            file_number = f"{FALLBACK_FILE_NUMBER}-{len(pdf_links) + 1}"
        # Already seen
        if file_number in seen_file_numbers:
            continue
        # Remember it
        seen_file_numbers.add(file_number)
        # The table row that holds the link
        parent_rows = link_element.xpath(PARENT_ROW_XPATH)
        # Clean text of its cells
        cell_texts = (
            [extract_clean_text(html_element=cell) for cell in parent_rows[0].xpath(PARENT_ROW_CELLS_XPATH)]
            if parent_rows
            else []
        )
        # Add the link with its description and date
        pdf_links.append(
            {
                "download_url": absolute_url,
                "file_number": file_number,
                "description": get_cell_text_or_empty(cell_texts=cell_texts, column_index=DOCUMENT_COLUMN_DESCRIPTION),
                "date_text": get_cell_text_or_empty(cell_texts=cell_texts, column_index=DOCUMENT_COLUMN_DATE),
            }
        )
    # Show how many PDF links were found
    logger.debug("Document page %s parsed: %d PDF links", page_url, len(pdf_links))
    # Show the details of every link
    for found_link in pdf_links:
        logger.debug(
            "  PDF link: file_number=%s date=%r description=%r url=%s",
            found_link["file_number"],
            found_link["date_text"],
            found_link["description"],
            found_link["download_url"],
        )
    # Hand the links back
    return pdf_links


# ===========================================================================
# FOLDER AND FILE NAMES
# ===========================================================================
# Make a string safe for use as part of a path (letters, digits and underscores only)
def sanitize_name_part(*, text: str | None, maximum_length: int, fallback: str) -> str:
    # Make sure it is a string
    raw_text = str(text or "")
    # Replace everything except A-Z, a-z, 0-9 and _ with _
    cleaned_text = re.sub(pattern=r"[^A-Za-z0-9_]", repl="_", string=raw_text)
    # Collapse repeated underscores and remove them at both ends
    cleaned_text = re.sub(pattern=r"_+", repl="_", string=cleaned_text).strip("_")
    # Cut to the maximum length
    shortened_text = cleaned_text[:maximum_length]
    # Did the cut split a word?
    cut_inside_a_word = len(cleaned_text) > maximum_length and cleaned_text[maximum_length] != "_"
    # A word was cut and a separator exists
    if cut_inside_a_word and "_" in shortened_text:
        # Trim back to the last separator
        shortened_text = shortened_text.rsplit("_", 1)[0]
    # The clean text, or the fallback if it is empty
    safe_text = shortened_text.strip("_") or fallback.replace("-", "_").strip("_")
    # Windows cannot create CON, NUL, COM1, ... (case-insensitive check)
    if safe_text.upper() in RESERVED_WINDOWS_NAMES:
        # Append _SAFE so the name is allowed
        safe_text += "_SAFE"
    # Hand the safe text back
    return safe_text


# Reformat a date string for use in file names
def format_date_for_file_name(*, date_text: str) -> str:
    # Try every known layout in turn
    for date_format in DATE_INPUT_FORMATS:
        # The layout may not fit
        try:
            # Parse and reformat
            return datetime.strptime(date_text.strip(), date_format).strftime(DATE_OUTPUT_FORMAT)
        # This layout does not fit
        except ValueError:
            # Try the next layout
            continue
    # No layout fits: use the sanitised raw text
    return sanitize_name_part(
        text=date_text, maximum_length=FILE_NUMBER_MAXIMUM_LENGTH, fallback=FALLBACK_DATE_TEXT
    )


# Folder name of a document
def build_folder_name(*, document_record: DocumentRecord) -> str:
    # Sanitised public ID, or a fallback built from the internal ID
    return sanitize_name_part(
        text=document_record["public_id"],
        maximum_length=FOLDER_NAME_MAXIMUM_LENGTH,
        fallback=FALLBACK_FOLDER_PREFIX + document_record["internal_id"],
    )


# File name of a PDF (the folder already carries the document ID)
def build_file_name(*, document_folder: Path, pdf_link: PdfLink) -> str:
    # The date as YYYY-MM-DD (or cleaned text), reduced to safe characters
    date_part = re.sub(
        pattern=r"[^A-Za-z0-9_]", repl="_", string=format_date_for_file_name(date_text=pdf_link["date_text"])
    )
    # Collapse repeated underscores and trim the ends
    date_part = re.sub(pattern=r"_+", repl="_", string=date_part).strip("_")
    # Sanitised file number
    number_part = sanitize_name_part(
        text=pdf_link["file_number"], maximum_length=FILE_NUMBER_MAXIMUM_LENGTH, fallback=FALLBACK_FILE_NUMBER
    )
    # Everything except the description (2 = the joining underscores)
    fixed_length = (
        len(date_part)
        + len(number_part)
        + 2
        + len(SUSPECT_MARKER)
        + len(PDF_FILE_EXTENSION)
        + len(PARTIAL_FILE_EXTENSION)
    )
    # Start with the normal limit
    description_limit = DESCRIPTION_MAXIMUM_LENGTH
    # Windows has a 260 character path limit
    if os.name == "nt":
        # Characters left for the description
        room_left = MAXIMUM_WINDOWS_PATH_LENGTH - len(str(document_folder.resolve())) - 1 - fixed_length
        # Shorten the description if needed (but never below the minimum)
        description_limit = max(MINIMUM_DESCRIPTION_LENGTH, min(description_limit, room_left))
    # Sanitised description
    description_part = sanitize_name_part(
        text=pdf_link["description"], maximum_length=description_limit, fallback=FALLBACK_DESCRIPTION
    )
    # Join the parts with underscores (empty parts are dropped), with no double underscores and none at the ends
    file_name_base = re.sub(r"_+", "_", "_".join(filter(None, [description_part, date_part, number_part]))).strip("_")
    # Add the extension
    file_name = file_name_base + PDF_FILE_EXTENSION
    # Sanity check (an AssertionError is caught per document)
    assert len(file_name) < 255, "file name too long"
    # Hand the name back
    return file_name


# The name used for PDFs that look incomplete, for example "x_2022-03-14_A1.suspect.pdf"
def build_suspect_path(*, target_path: Path) -> Path:
    return target_path.with_name(target_path.stem + SUSPECT_MARKER + PDF_FILE_EXTENSION)


# ===========================================================================
# STATUS CODES AND CONTENT CHECKS
# ===========================================================================
# Convert a Retry-After header into seconds (None when it is missing or unreadable)
def parse_retry_after_seconds(*, header_value: str | None) -> int | None:
    # The server sent no such header
    if not header_value:
        return None
    # Remove stray spaces
    stripped_value = header_value.strip()
    # A plain number of seconds
    if stripped_value.isdigit():
        # Convert it
        seconds = int(stripped_value)
    # Possibly a date such as "Wed, 21 Oct 2026 07:28:00 GMT"
    else:
        # The date may be unreadable
        try:
            # Parse the HTTP date
            retry_moment = parsedate_to_datetime(data=stripped_value)
            # A date without a time zone is UTC (comparing it with local time would be wrong by hours)
            if retry_moment.tzinfo is None:
                # Mark it as UTC
                retry_moment = retry_moment.replace(tzinfo=timezone.utc)
            # Seconds from now (in UTC) until then
            seconds = int((retry_moment - datetime.now(tz=timezone.utc)).total_seconds())
        # Something we do not handle
        except (TypeError, ValueError, OverflowError):
            # Fall back to the normal backoff
            return None
    # Keep the value between 0 and the maximum
    return max(0, min(seconds, MAXIMUM_RETRY_AFTER_SECONDS))


# Turn bad HTTP status codes into the right error
def check_http_status(*, status_code: int, status_text: str, headers: dict[str, str], label: str) -> None:
    # Look at the status code
    match status_code:
        # Not logged in (any more)
        case 401:
            raise SessionExpiredError(f"{label}: HTTP 401 - not logged in")
        # This one page or file is off limits
        case 403:
            raise ForbiddenError(f"{label}: HTTP 403 - access denied")
        # 408, 429, 5xx, ...: probably temporary
        case code if code in HTTP_RETRYABLE_STATUSES:
            # Respect Retry-After if given
            wait_seconds = parse_retry_after_seconds(header_value=headers.get(RETRY_AFTER_HEADER_NAME))
            # Ask the caller to retry later
            raise RetryableFetchError(f"{label}: HTTP {code} {status_text} (temporary)", wait_seconds)
        # Success, but no content
        case 204:
            raise PermanentFetchError(f"{label}: HTTP 204 - the server sent no content")
        # Any other non-2xx status (404, 410, 400, ...): retrying will not help
        case code if not 200 <= code < 300:
            raise PermanentFetchError(f"{label}: HTTP {code} {status_text}")
        # The normal 2xx case
        case _:
            pass


# Guess whether the server sent a login page
def looks_like_login_page(*, page_body: bytes) -> bool:
    # Only the beginning, in lower case
    start_of_body = page_body[:LOGIN_PAGE_SEARCH_BYTES].lower()
    # Does the word "login" appear?
    has_login_word = LOGIN_WORD_MARKER in start_of_body
    # Only logged-in pages have a logout link
    has_logout_link = LOGOUT_LINK_MARKER.encode() in page_body.lower()
    # A login page mentions "login" but has no logout link
    return has_login_word and not has_logout_link


# Check a document page is real and logged in
def validate_document_page(*, page_body: bytes) -> None:
    # The page is empty: usually a glitch
    if not page_body.strip():
        raise RetryableFetchError("the document page was empty")
    # No logout link means we were logged out
    if LOGOUT_LINK_MARKER.encode() not in page_body.lower():
        raise SessionExpiredError("the document page has no Logout link")


# Decide WHY a file is not a PDF (always raises)
def raise_error_for_non_pdf(*, file_body: bytes, description: str) -> None:
    # It is really a login page
    if looks_like_login_page(page_body=file_body):
        raise SessionExpiredError("got a login page instead of a PDF")
    # The body is empty: treat as a glitch
    if not file_body.strip():
        raise RetryableFetchError("the PDF response was empty")
    # Anything else: retrying will not help
    raise PermanentFetchError(f"the downloaded file is not a PDF ({description}, {len(file_body)} bytes)")


# ===========================================================================
# CHROME TABS AND THE RETRY ENGINE
# ===========================================================================
# The first open tab (the results grid tab)
def get_first_open_tab(*, browser_context: BrowserContext) -> Page:
    # Look at every tab
    for open_tab in browser_context.pages:
        # Skip closed tabs
        if not open_tab.is_closed():
            return open_tab
    # No open tab left, so open a new one
    return browser_context.new_page()


# The calling thread's own tab for document pages and downloads (the results grid tab is never touched)
def get_download_tab(*, browser_context: BrowserContext) -> Page:
    # The tab this thread opened earlier (each thread has its own)
    download_tab = getattr(thread_local, "download_tab", None)
    # Missing, closed or from a crashed session
    if download_tab is None or download_tab.is_closed() or download_tab.context is not browser_context:
        # Report it
        logger.debug("Opening a new download tab")
        # Open a fresh TAB in the one Chrome window (never a new window)
        download_tab = browser_context.new_page()
        # Worker tabs are created over the worker's own connection, so the worker itself watches them
        if threading.current_thread() is not threading.main_thread():
            # A crash of this tab restarts Chrome
            attach_tab_crash_handler(download_tab)
            # Accept dialogs of this tab automatically
            download_tab.on(event="dialog", f=accept_browser_dialog)
        # Remember it for this thread only
        thread_local.download_tab = download_tab
    # Hand the tab back
    return download_tab


# Visit a page in Chrome and return (response, page HTML as bytes, final URL)
def open_url_in_download_tab(*, browser_context: BrowserContext, url: str, label: str) -> tuple[Response, bytes, str]:
    # Log the address
    logger.info(f"{label}: Opening {url}")
    # Remember when the visit started
    started_at = time.monotonic()
    # The dedicated tab
    download_tab = get_download_tab(browser_context=browser_context)
    # Let Chrome load the page like a person
    response = download_tab.goto(url=url, wait_until="domcontentloaded", timeout=REQUEST_TIMEOUT_MS)
    # The navigation was replaced by another one
    if response is None:
        raise RetryableFetchError(f"{label}: Chrome gave no response")
    # The HTML as Chrome rendered it
    page_body = download_tab.content().encode(encoding="utf-8")
    # Log status, size and duration
    logger.info(
        f"{label}: HTTP {response.status} {response.status_text}, {len(page_body)} bytes "
        f"in {time.monotonic() - started_at:.1f}s"
    )
    # The response, the page and where Chrome ended up
    return response, page_body, download_tab.url


# Does a tab that is already open show the logout link? (makes no request)
def tab_shows_logout_link(*, tab: Page) -> bool:
    # The tab may be closed or busy
    try:
        # Only logged-in pages contain the logout link
        return LOGOUT_LINK_MARKER in tab.content().lower()
    # The tab cannot be inspected
    except PlaywrightError:
        # Treat it as "not shown"
        return False


# Restart Chrome (and log in again) when the RESULTS TAB shows a log-in form or lost its grid (makes no request)
def raise_if_results_tab_logged_out(*, tab: Page, label: str) -> None:
    # Looking at the tab can fail
    try:
        # A password field means the site sent us back to the log-in page
        login_form_shown = tab.locator(selector=PASSWORD_FIELD_SELECTOR).count() > 0
        # A logged-out results page has no grid
        grid_missing = tab.locator(selector=RESULTS_GRID_SELECTOR).count() == 0
    # The tab cannot be inspected
    except PlaywrightError as error:
        # A dead Chrome must restart
        restart_browser_if_error_is_fatal(error)
        # Otherwise there is no proof of a logout
        return
    # The session really ended
    if login_form_shown or grid_missing:
        # Close Chrome, start it again, log in again and resume
        raise BrowserLostError(f"{label}: logged out")


# The ONE retry loop every download uses
def run_with_retries(*, browser_context: BrowserContext, label: str, attempt_function: AttemptFunction) -> Any:
    # The most recent problem, for the final message
    last_problem = "unknown problem"
    # Normal attempts made so far
    attempt_number = 0
    # Try up to RETRY_ATTEMPTS times
    while attempt_number < RETRY_ATTEMPTS:
        # Count this attempt
        attempt_number += 1
        # Do not retry on a dead browser
        raise_if_browser_unusable()
        # Progressive backoff: 2s, 4s, 6s, ...
        backoff_seconds = SECONDS_PER_RETRY_STEP * attempt_number
        # Catch every kind of problem
        try:
            # Do the actual work
            result = attempt_function(attempt_label=f"{label} (try {attempt_number}/{RETRY_ATTEMPTS})")
            # The server is happy, so move the delay back towards normal
            request_delay.decrease()
            # Everything is fine
            return result
        # The site says we are not logged in (no extra request is made to check)
        except SessionExpiredError as error:
            # Remember the problem
            last_problem = str(error)
            # Tell the user
            logger.warning(f"{label}: {error}")
            # Count how often this request met a logout (the dictionary is shared by all workers)
            with state_lock:
                # One more logout for this label
                expired_count = run_state.session_expired_counts.get(label, 0) + 1
                # Remember it
                run_state.session_expired_counts[label] = expired_count
            # Logging in again did not help: stop restarting Chrome forever
            if expired_count > 1:
                raise PermanentFetchError(f"{label}: still not logged in after logging in again") from error
            # Close Chrome, start it again, log in again and resume
            raise BrowserLostError(f"{label}: logged out") from error
        # This one page or file is off limits: retrying will not help
        except ForbiddenError as error:
            # Pass it up as a permanent problem
            raise PermanentFetchError(f"{label}: access denied (HTTP 403)") from error
        # A temporary server problem
        except RetryableFetchError as error:
            # Remember the problem
            last_problem = str(error)
            # Prefer the server's wait time
            backoff_seconds = error.wait_seconds if error.wait_seconds is not None else backoff_seconds
            # Be gentler to the server from now on
            request_delay.increase()
            # Tell the user
            logger.warning(f"{label}: {error}; waiting {backoff_seconds}s")
        # Retrying cannot fix this
        except PermanentFetchError:
            # Pass it straight up
            raise
        # Network error, timeout, crash or closed browser
        except PlaywrightError as error:
            # Remember the problem
            last_problem = f"network error: {error}"
            # Chrome is closed, crashed or disconnected: restart it now
            restart_browser_if_error_is_fatal(error)
            # Tell the user
            logger.warning(f"{label}: {last_problem}; waiting {backoff_seconds}s")
        # Only wait if another attempt follows
        if attempt_number < RETRY_ATTEMPTS:
            # Wait before trying again
            sleep_unless_session_aborted(seconds=backoff_seconds)
    # All attempts failed
    raise PermanentFetchError(f"{label}: gave up after {RETRY_ATTEMPTS} tries ({last_problem})")


# Visit a page in Chrome safely (status check, content check and retries)
def fetch_page_with_retries(
    *, browser_context: BrowserContext, url: str, label: str, validate_page: PageValidator
) -> tuple[str, bytes]:
    # One attempt
    def attempt(*, attempt_label: str) -> tuple[str, bytes]:
        # Let Chrome visit the page
        response, page_body, final_url = open_url_in_download_tab(
            browser_context=browser_context, url=url, label=attempt_label
        )
        # Check the status code
        check_http_status(
            status_code=response.status, status_text=response.status_text, headers=response.headers, label=label
        )
        # Check the content is what we expected
        validate_page(page_body=page_body)
        # Hand the result back
        return final_url, page_body

    # Run it with all the retry rules
    return run_with_retries(browser_context=browser_context, label=label, attempt_function=attempt)


# Pause the run when many documents fail in a row (safe to call from several worker threads)
def apply_circuit_breaker(*, document_ok: bool) -> None:
    # A success resets everything
    if document_ok:
        # Only one thread at a time may change the counters
        with state_lock:
            # No streak
            run_state.consecutive_document_failures = 0
            # The next trip starts with a short pause again
            run_state.circuit_breaker_trips = 0
        return
    # Count one more failure in a row and remember the new streak length
    with state_lock:
        run_state.consecutive_document_failures += 1
        failure_streak = run_state.consecutive_document_failures
    # Show how close the breaker is to tripping
    logger.debug("Failure streak: %d/%d", failure_streak, CIRCUIT_BREAKER_FAILURES)
    # Not enough failures yet
    if failure_streak < CIRCUIT_BREAKER_FAILURES:
        return
    # Only one worker pauses; the other workers wait for it at the start of their next document
    with circuit_breaker_lock:
        # Count this trip (unless another worker already paused and reset the streak)
        with state_lock:
            # Another worker already handled this streak
            if run_state.consecutive_document_failures < CIRCUIT_BREAKER_FAILURES:
                return
            # Count this trip
            run_state.circuit_breaker_trips += 1
            trip_number = run_state.circuit_breaker_trips
            streak_now = run_state.consecutive_document_failures
        # Longer pause with every trip
        wait_seconds = min(MAXIMUM_BACKOFF_SECONDS, CIRCUIT_BREAKER_BASE_SECONDS * 2 ** (trip_number - 1))
        # Explain
        logger.error(
            f"{streak_now} documents in a row failed (site down, throttling or session lost?). "
            f"Pausing {wait_seconds}s (no extra requests are made)"
        )
        # Wait, but wake up at once when the session is aborted (so a restart never hangs on this lock)
        session_abort_event.wait(timeout=wait_seconds)
        # Chrome died or the internet went down during the pause
        raise_if_browser_unusable()
        # Start counting again
        with state_lock:
            run_state.consecutive_document_failures = 0
    # Tell the user
    logger.info("Resuming after the pause")


# ===========================================================================
# CHROME DOWNLOADS
# ===========================================================================
# Check a file saved by Chrome and describe it (raises if it is not a PDF)
def inspect_downloaded_pdf(*, partial_path: Path) -> DownloadedPdf:
    # Number of bytes on disk
    size_bytes = partial_path.stat().st_size
    # Open the file for reading
    with partial_path.open(mode="rb") as saved_file:
        # Read the beginning
        file_head = saved_file.read(PDF_SIGNATURE_SEARCH_BYTES)
        # Not a PDF
        if PDF_SIGNATURE not in file_head:
            # Read enough to recognise a login page
            file_body = file_head + saved_file.read(MAXIMUM_NON_PDF_BYTES)
            # Always raises
            raise_error_for_non_pdf(file_body=file_body, description="saved by Chrome")
        # Jump near the end of the file
        saved_file.seek(max(0, size_bytes - PDF_END_SEARCH_BYTES))
        # Read the last bytes
        file_tail = saved_file.read()
        # Back to the start for the hash
        saved_file.seek(0)
        # Running hash of the file
        digest = hashlib.sha256()
        # Read in pieces
        while chunk := saved_file.read(HASH_CHUNK_BYTES):
            # Hash the piece
            digest.update(chunk)
    # Describe the file
    return DownloadedPdf(
        size_bytes=size_bytes,
        sha256_hex=digest.hexdigest(),
        has_end_marker=PDF_END_MARKER in file_tail,
        partial_path=partial_path,
    )


# Turn Chrome's failure text into the right error
def raise_error_for_download_failure(*, failure: str, label: str) -> None:
    # Chrome reports exactly this when the server answered with an error status (403, 404, 500, ...)
    if failure.lower() == "canceled":
        # One attempt is enough
        raise RefusedDownloadError(
            f"{label}: the server refused the download "
            f"(Chrome only reports '{failure}'; probably HTTP 403, 404 or 500)"
        )
    # Anything else may be temporary
    raise RetryableFetchError(f"{label}: Chrome download failed ({failure})")


# Make Chrome download one file and wait for it to finish
def trigger_chrome_download(*, download_tab: Page, url: str, label: str) -> Download:
    # Wait for Chrome to start a download
    with download_tab.expect_download(timeout=REQUEST_TIMEOUT_MS) as download_info:
        # Click the real link (as a download link)
        download_tab.evaluate(expression=CLICK_DOWNLOAD_JAVASCRIPT, arg=url)
    # The download Chrome started
    download = download_info.value
    # Waits for the end; None means success
    failure = download.failure()
    # Report how it ended
    logger.debug("%s: Chrome download finished, failure=%r", label, failure)
    # Chrome could not finish the download
    if failure:
        # Raise the matching error
        raise_error_for_download_failure(failure=failure, label=label)
    # Hand the finished download back
    return download


# A download got 403/404/410 (or Chrome said "canceled"): the file is refused (always raises; makes NO extra request)
def confirm_download_was_refused(*, label: str, reason: str) -> None:
    # A dead browser is not a refusal
    raise_if_browser_unusable()
    # A logged-out session would have answered 401 or sent a login page, which is handled elsewhere
    raise RefusedDownloadError(f"{label}: the server refused the download ({reason})")


# Download one file INSIDE Chrome with a hard timeout and write it to partial_path (raises on every kind of failure)
def download_pdf_by_fetch(
    *, browser_context: BrowserContext, download_tab: Page, url: str, label: str, partial_path: Path
) -> None:
    # Chrome downloads the whole file; the JavaScript timer guarantees that this call always ends
    fetch_result = download_tab.evaluate(
        expression=FETCH_PDF_JAVASCRIPT, arg={"url": url, "timeoutMs": DOWNLOAD_TIMEOUT_MS}
    )
    # Whatever happens next, the bytes must not stay in the page's memory
    try:
        # The HTTP status code the server answered with
        status_code = int(fetch_result.get("status", 0))
        # The file is really refused
        if status_code in (403, 404, 410):
            # Always raises
            confirm_download_was_refused(label=label, reason=f"HTTP {status_code}")
        # Turn every other bad status into the right error (401 logout, 429/5xx retry, ...)
        check_http_status(
            status_code=status_code,
            status_text=str(fetch_result.get("statusText", "")),
            headers={RETRY_AFTER_HEADER_NAME: str(fetch_result.get("retryAfter", ""))},
            label=label,
        )
        # How many bytes Chrome received
        total_bytes = int(fetch_result.get("size", 0))
        # How many bytes the server announced
        announced_length = str(fetch_result.get("contentLength", ""))
        # With compression the announced length is the compressed size, so only compare uncompressed answers
        if announced_length.isdigit() and not fetch_result.get("contentEncoding") and int(announced_length) != total_bytes:
            # The connection ended early: download it again
            raise RetryableFetchError(
                f"{label}: incomplete download ({total_bytes} of {announced_length} bytes received)"
            )
        # Write the temporary file piece by piece
        with partial_path.open(mode="wb") as partial_file:
            # Where the next piece starts
            offset = 0
            # Until every byte was moved
            while offset < total_bytes:
                # The size of this piece
                chunk_length = min(DOWNLOAD_CHUNK_BYTES, total_bytes - offset)
                # Ask Chrome for the piece (base64 text)
                encoded_chunk = download_tab.evaluate(expression=READ_PDF_CHUNK_JAVASCRIPT, arg=[offset, chunk_length])
                # The page lost the bytes (for example because it reloaded)
                if not isinstance(encoded_chunk, str):
                    raise RetryableFetchError(f"{label}: Chrome lost the downloaded bytes at offset {offset}")
                # Decode the piece
                chunk = base64.b64decode(encoded_chunk)
                # A piece of the wrong size means something went wrong in between
                if len(chunk) != chunk_length:
                    raise RetryableFetchError(f"{label}: a piece of the file arrived with the wrong size")
                # Save the piece
                partial_file.write(chunk)
                # Move on
                offset += chunk_length
    # Always runs
    finally:
        # Free the memory in the page (the page may be gone already)
        try:
            # Remove the bytes
            download_tab.evaluate(expression=DISCARD_PDF_JAVASCRIPT)
        # The tab or the browser is gone
        except PlaywrightError:
            pass


# Download one file with Chrome's own download event and save it to partial_path (the "click" method)
def download_pdf_by_clicking(
    *, browser_context: BrowserContext, download_tab: Page, url: str, label: str, partial_path: Path
) -> None:
    # Chrome reports "canceled" for 403, 404 and 500 alike
    try:
        # Let Chrome download the file
        download = trigger_chrome_download(download_tab=download_tab, url=url, label=label)
    # Is this a real refusal?
    except RefusedDownloadError:
        # Always raises
        confirm_download_was_refused(label=label, reason="Chrome reported 'canceled'")
    # Copy Chrome's temporary download to our temporary file
    download.save_as(path=str(partial_path))


# Let CHROME download one PDF to a temporary file (no page is reloaded; the tab already shows the document page)
def download_pdf_with_retries(
    *,
    browser_context: BrowserContext,
    url: str,
    label: str,
    document_page_url: str,
    partial_path: Path,
) -> DownloadedPdf:
    # How many downloads without the %%EOF end marker were thrown away and downloaded again
    incomplete_downloads = 0

    # One attempt
    def attempt(*, attempt_label: str) -> DownloadedPdf:
        # This variable lives in the enclosing function
        nonlocal incomplete_downloads
        # The calling thread's own tab
        download_tab = get_download_tab(browser_context=browser_context)
        # The tab already shows the document page (load_pdf_links opened it). Only a new or crashed tab (about:blank)
        # needs the page, because fetch() must run on the same site (same origin) to send the log-in cookies
        if not download_tab.url.startswith(BASE_URL):
            # Report it
            logger.debug("%s: the tab is not on the site, opening the document page %s", attempt_label, document_page_url)
            # Open the document page in this tab
            download_tab.goto(url=document_page_url, wait_until="domcontentloaded", timeout=REQUEST_TIMEOUT_MS)
        # Make sure there is room (waits instead of stopping)
        wait_for_free_disk_space()
        # Make sure the folder exists
        partial_path.parent.mkdir(parents=True, exist_ok=True)
        # Leftovers of a failed attempt must never be mistaken for new data
        delete_file_quietly(file_path=partial_path)
        # Log the address
        logger.info(f"{attempt_label}: Chrome is downloading {url} (method: {DOWNLOAD_METHOD})")
        # Remember when the download started
        started_at = time.monotonic()
        # The chosen download method
        if DOWNLOAD_METHOD == "click":
            # Chrome's own download event
            download_pdf_by_clicking(
                browser_context=browser_context,
                download_tab=download_tab,
                url=url,
                label=label,
                partial_path=partial_path,
            )
        # The recommended method
        else:
            # Download inside Chrome with a hard timeout
            download_pdf_by_fetch(
                browser_context=browser_context,
                download_tab=download_tab,
                url=url,
                label=label,
                partial_path=partial_path,
            )
        # A rejected file must never stay behind
        try:
            # Check it really is a PDF
            downloaded_pdf = inspect_downloaded_pdf(partial_path=partial_path)
        # Anything went wrong (including Ctrl+C)
        except BaseException:
            # Remove the rejected file
            delete_file_quietly(file_path=partial_path)
            # Pass the problem on
            raise
        # Optional: a file without %%EOF is thrown away and downloaded again (off by default: same URL twice)
        if not downloaded_pdf.has_end_marker and incomplete_downloads < INCOMPLETE_DOWNLOAD_REDOWNLOADS:
            # Count this thrown-away download
            incomplete_downloads += 1
            # Remove the incomplete-looking file
            delete_file_quietly(file_path=partial_path)
            # Download it again (the last attempt accepts the file and saves it as suspect)
            raise RetryableFetchError(
                f"{attempt_label}: the PDF has no %%EOF end marker, so it looks incomplete "
                f"(download {incomplete_downloads}/{INCOMPLETE_DOWNLOAD_REDOWNLOADS}); downloading it again"
            )
        # Log size and duration
        logger.info(
            f"{attempt_label}: Chrome saved {downloaded_pdf.size_bytes} bytes in {time.monotonic() - started_at:.1f}s"
        )
        # Hand the result back
        return downloaded_pdf

    # Run it with all the retry rules
    return run_with_retries(browser_context=browser_context, label=label, attempt_function=attempt)


# ===========================================================================
# BROWSER AND KEYBOARD
# ===========================================================================
# Automatically accept browser alerts (Playwright passes the dialog positionally)
def accept_browser_dialog(dialog: Dialog) -> None:
    # The dialog may vanish before we answer it
    try:
        # Log the alert text so it is never a mystery
        logger.info(f"Browser alert accepted: {dialog.message[:100]!r}")
        # Click OK
        dialog.accept()
    # The dialog was already gone, or the main thread's connection answered it first
    except PlaywrightError as error:
        # Several connections watch the same tab, so "already handled" is normal and only logged quietly
        if "already handled" in str(error).lower():
            logger.debug(f"The browser alert was already answered: {error}")
        # Anything else is worth a warning
        else:
            logger.warning(f"Could not answer a browser alert: {error}")


# Make Chrome believe it closed cleanly and forget the old tabs, so it never restores them
def clear_chrome_restore_state() -> None:
    # The folder of Chrome's default profile
    default_profile_folder = BROWSER_PROFILE_FOLDER / "Default"
    # Delete the saved tabs of the last session
    shutil.rmtree(default_profile_folder / "Sessions", ignore_errors=True)
    # Chrome's settings file
    preferences_file = default_profile_folder / "Preferences"
    # Reading and writing the file can fail
    try:
        # First start: there is nothing to fix
        if not preferences_file.exists():
            return
        # Read the settings
        preferences = json.loads(preferences_file.read_text(encoding="utf-8"))
        # The profile section of the settings
        profile_settings = preferences.setdefault("profile", {})
        # Pretend the last exit was normal and clean
        profile_settings["exit_type"] = "Normal"
        profile_settings["exited_cleanly"] = True
        # Save the settings
        preferences_file.write_text(json.dumps(preferences), encoding="utf-8")
        # Report it
        logger.debug("Chrome restore state cleared")
    # The file is unreadable or not valid JSON
    except (OSError, ValueError) as error:
        # Carry on without it
        logger.warning(f"Could not reset Chrome's restore state: {error}")


# Ask the operating system for a free local TCP port
def find_free_local_port() -> int:
    # A throw-away socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe_socket:
        # Port 0 means "any free port"
        probe_socket.bind((REMOTE_DEBUGGING_HOST, 0))
        # The port the operating system chose
        return probe_socket.getsockname()[1]


# Launch and configure the browser
def launch_browser(*, playwright_instance: Playwright) -> BrowserContext:
    # Create the browser profile folder
    BROWSER_PROFILE_FOLDER.mkdir(parents=True, exist_ok=True)
    # Chrome must not offer to restore tabs from a crashed session
    clear_chrome_restore_state()
    # Report the launch
    logger.info(f"Launching Chromium with profile {BROWSER_PROFILE_FOLDER}")
    # The port on which the worker threads connect to this same Chrome
    run_state.remote_debugging_port = find_free_local_port()
    # The browser may not be installed
    try:
        # Launch the persistent browser (visible, maximized, password manager on)
        browser_context = playwright_instance.chromium.launch_persistent_context(
            user_data_dir=str(BROWSER_PROFILE_FOLDER),
            headless=not SHOW_BROWSER_WINDOW,
            accept_downloads=True,
            no_viewport=True,
            args=[*BROWSER_LAUNCH_ARGUMENTS, f"--remote-debugging-port={run_state.remote_debugging_port}"],
            ignore_default_args=BROWSER_IGNORED_DEFAULT_ARGUMENTS,
        )
    # Chromium could not start
    except PlaywrightError as error:
        # The main loop retries later
        raise RuntimeError(
            f"Could not start Chromium ({error}). Try: playwright install --with-deps chromium"
        ) from error
    # Every tab except the first can only be a restored tab
    for restored_tab in browser_context.pages[1:]:
        # Closing can fail
        try:
            # Report it
            logger.debug("Closing a restored tab: %s", restored_tab.url)
            # Close the restored tab
            restored_tab.close()
        # The tab is already gone
        except PlaywrightError:
            pass
    # A fresh browser is running
    run_state.browser_is_closed = False
    # The last session ended in a redirect loop
    if run_state.clear_cookies_on_next_start:
        # Clearing can fail
        try:
            # Delete the broken session cookies (the saved password logs in again)
            browser_context.clear_cookies()
            # Tell the user
            logger.info("Deleted the old site cookies, logging in fresh")
        # Chrome refused
        except PlaywrightError as error:
            # Carry on
            logger.warning(f"Could not delete the cookies: {error}")
        # Done
        run_state.clear_cookies_on_next_start = False
    # Only close events of THIS context count from now on
    run_state.active_browser_context = browser_context
    # Only when enabled: every page of the site keeps the session alive (it repeats a request, so it is off by default)
    if KEEP_ALIVE_ENABLED:
        # Add the keep-alive script to every page
        browser_context.add_init_script(script=KEEP_ALIVE_JAVASCRIPT)
    # Find out the moment Chrome dies
    browser_context.on(event="close", f=handle_browser_context_closed)
    # Accept dialogs automatically
    browser_context.on(event="dialog", f=accept_browser_dialog)
    # Every tab that is open right now
    for existing_tab in browser_context.pages:
        # A crash of this tab restarts Chrome
        watch_tab_for_crash(existing_tab)
    # Tabs that open later are watched too
    browser_context.on(event="page", f=watch_tab_for_crash)
    # Log all requests and responses of this browser
    attach_network_logging(browser_context)
    # Tell the user
    logger.info("Browser started; logging all requests, responses, console output and downloads")
    # Hand the browser back
    return browser_context


# The loop of the keyboard thread (works on Linux, macOS and Windows)
def read_keyboard_lines() -> None:
    # Keep reading lines
    while True:
        # Reading can fail
        try:
            # Wait for a line (blocks only this thread)
            typed_line = sys.stdin.readline()
        # The input stream is broken
        except (OSError, ValueError):
            # Treat it like the end of the input
            typed_line = ""
        # An empty string means the input has ended
        if not typed_line:
            # Remember that no more input will come
            keyboard_reader_state["closed"] = True
            # End the thread
            return
        # Show every typed line
        logger.debug("Keyboard input received: %r", typed_line)
        # Hand the line to the main thread
        keyboard_lines.put(item=typed_line)


# Start the keyboard thread once
def start_keyboard_reader() -> None:
    # The thread already exists
    if keyboard_reader_state["started"]:
        return
    # Remember that the thread now exists
    keyboard_reader_state["started"] = True
    # Daemon: never blocks the exit
    threading.Thread(target=read_keyboard_lines, name="keyboard-reader", daemon=True).start()


# Let Playwright process events for a short moment
def let_browser_process_events(*, tab: Page) -> None:
    # The page may be closed
    try:
        # Let Playwright process events
        tab.wait_for_timeout(timeout=KEYBOARD_POLL_MS)
    # The page is closed or the browser is gone
    except PlaywrightError:
        # Just sleep instead
        time.sleep(KEYBOARD_POLL_MS / 1000)


# Remove one typed line from the queue; True if there was one
def consume_typed_line() -> bool:
    # The queue may be empty
    try:
        # Take one line
        keyboard_lines.get_nowait()
    # Nothing typed
    except queue.Empty:
        return False
    # A line was typed
    return True


# Block until the user presses Enter (keeps the browser alive and nags every few minutes)
def wait_for_user_enter(*, tab: Page, message: str) -> None:
    # Make sure the keyboard thread runs
    start_keyboard_reader()
    # Throw away Enter presses that happened before this prompt
    while consume_typed_line():
        pass
    # Show the prompt
    print(message, flush=True)
    # Ring the terminal bell
    ring_terminal_bell()
    # When the prompt was last shown
    last_reminder_at = time.monotonic()
    # Wait for Enter
    while True:
        # Chrome died or the internet went down while waiting
        raise_if_browser_unusable()
        # Enter was pressed
        if consume_typed_line():
            return
        # The terminal input has ended, so nobody can press Enter
        if keyboard_reader_state["closed"]:
            # Explain
            logger.warning(f"Terminal input is closed; continuing automatically in {CLOSED_INPUT_WAIT_SECONDS}s")
            # Give the browser some time
            sleep_and_log(seconds=CLOSED_INPUT_WAIT_SECONDS)
            # Continue as if Enter was pressed
            return
        # Nobody answered for a while
        if time.monotonic() - last_reminder_at >= PROMPT_REPEAT_SECONDS:
            # Show the prompt again
            print(f"\n(still waiting for you){message}", flush=True)
            # Ring the terminal bell again
            ring_terminal_bell()
            # Restart the reminder timer
            last_reminder_at = time.monotonic()
        # Let Playwright process events
        let_browser_process_events(tab=tab)


# ===========================================================================
# LOG-IN AND SEARCH
# ===========================================================================
# Find the tab that shows the results grid
def find_results_tab(*, browser_context: BrowserContext) -> Page | None:
    # Every open tab
    for open_tab in browser_context.pages:
        # A tab can close while we look at it
        try:
            # The grid exists on this tab
            if open_tab.locator(selector=RESULTS_GRID_SELECTOR).count() > 0:
                return open_tab
        # This tab cannot be inspected
        except PlaywrightError:
            # Look at the next tab
            continue
    # No tab shows the results grid
    return None


# Did Chrome autofill the saved password into the log-in form?
def is_password_autofilled(*, tab: Page) -> bool:
    # The page may be busy or closed
    try:
        # Chrome marks autofilled fields (it hides their value from scripts until a click)
        return tab.locator(selector=PASSWORD_AUTOFILLED_SELECTOR).count() > 0
    # The page cannot be inspected
    except PlaywrightError:
        # Treat it as "not filled"
        return False


# Log in with the password saved in Chrome; True if we are (probably) logged in
def try_automatic_login(*, tab: Page) -> bool:
    # Already logged in
    if tab_shows_logout_link(tab=tab):
        # Tell the user
        logger.info("Already logged in")
        return True
    # Counting elements can fail
    try:
        # Is there a password field on the page?
        has_login_form = tab.locator(selector=PASSWORD_FIELD_SELECTOR).count() > 0
    # The page cannot be inspected
    except PlaywrightError:
        # Treat it as "no form"
        has_login_form = False
    # No log-in form is shown
    if not has_login_form:
        # Tell the user
        logger.info("No log-in form is shown, assuming we are already logged in")
        # The search step will show whether this is true
        return True
    # Moment to stop waiting for the autofill
    deadline = time.monotonic() + AUTO_LOGIN_WAIT_SECONDS
    # Report the wait
    logger.debug("Waiting up to %ss for Chrome to autofill the saved password", AUTO_LOGIN_WAIT_SECONDS)
    # Wait for Chrome to fill the form
    while not is_password_autofilled(tab=tab):
        # Chrome did not fill anything in time
        if time.monotonic() >= deadline:
            # Explain
            logger.warning(
                "Chrome did not autofill a saved password (log in once in this browser and let Chrome save the password)"
            )
            # Fall back to the manual log-in
            return False
        # Let Playwright process events
        let_browser_process_events(tab=tab)
    # Tell the user
    logger.info("Chrome filled in the saved log-in, submitting it")
    # Clicking and waiting can fail
    try:
        # A real click lets Chrome release the autofilled password to the page
        tab.click(selector=PASSWORD_FIELD_SELECTOR, timeout=AUTO_STEP_TIMEOUT_MS)
        # Press Enter to submit the form
        tab.press(selector=PASSWORD_FIELD_SELECTOR, key="Enter", timeout=AUTO_STEP_TIMEOUT_MS)
        # Wait until the log-in form is gone
        tab.wait_for_function(expression=LOGIN_DONE_JAVASCRIPT, timeout=AUTO_STEP_TIMEOUT_MS)
    # Click, key press or wait failed
    except PlaywrightError as error:
        # A dead Chrome must restart
        restart_browser_if_error_is_fatal(error)
        # Explain
        logger.warning(f"Automatic log-in failed: {error}")
        # Fall back to the manual log-in
        return False
    # Tell the user
    logger.info("Automatic log-in OK")
    return True


# Open the search page, choose Advanced Search, click Search; returns the results tab or None
def run_advanced_search(*, browser_context: BrowserContext, tab: Page) -> Page | None:
    # The page may not load
    try:
        # Open the search page
        tab.goto(url=SEARCH_PAGE_URL, wait_until="domcontentloaded", timeout=REQUEST_TIMEOUT_MS)
    # The page did not load
    except PlaywrightError as error:
        # A dead Chrome must restart
        restart_browser_if_error_is_fatal(error)
        # Explain
        logger.warning(f"Could not open the search page: {error}")
        # Fall back to the manual steps
        return None
    # The two optional clicks (the page may already be the advanced search)
    for step_pattern in (SEARCH_METHOD_PATTERN, ADVANCED_SEARCH_PATTERN):
        # The control may not exist
        try:
            # Click the control with this text
            tab.get_by_text(step_pattern).first.click(timeout=AUTO_OPTIONAL_CLICK_TIMEOUT_MS)
            # Tell the user
            logger.info(f"Clicked '{step_pattern.pattern}'")
        # The control was not found or not clickable
        except PlaywrightError:
            # Not an error: the page may already show it
            logger.info(f"'{step_pattern.pattern}' not found, continuing")
    # The Search button may not be found by its role
    try:
        # Click the Search button
        tab.get_by_role("button", name=SEARCH_BUTTON_PATTERN).first.click(timeout=AUTO_STEP_TIMEOUT_MS)
    # Not found that way
    except PlaywrightError:
        # Try the backup selector
        try:
            # Click the Search button
            tab.locator(selector=SEARCH_BUTTON_FALLBACK_SELECTOR).first.click(timeout=AUTO_STEP_TIMEOUT_MS)
        # Not found at all
        except PlaywrightError as error:
            # A dead Chrome must restart
            restart_browser_if_error_is_fatal(error)
            # Explain
            logger.warning(f"Could not click Search: {error}")
            # Fall back to the manual steps
            return None
    # Tell the user
    logger.info("Clicked Search, waiting for the results grid")
    # Moment to stop waiting for the grid
    deadline = time.monotonic() + REQUEST_TIMEOUT_MS / 1000
    # Until the grid shows up
    while time.monotonic() < deadline:
        # Chrome died or the internet went down while waiting
        raise_if_browser_unusable()
        # Look for the grid on every tab
        results_tab = find_results_tab(browser_context=browser_context)
        # Found it
        if results_tab is not None:
            return results_tab
        # Let Playwright process events
        let_browser_process_events(tab=tab)
    # Explain
    logger.warning("The results grid did not appear after clicking Search")
    # Fall back to the manual steps
    return None


# Log in and search automatically; fall back to asking the user if that fails
def open_results_grid(*, browser_context: BrowserContext) -> Page:
    # The first tab
    first_tab = get_first_open_tab(browser_context=browser_context)
    # The start page may not load
    try:
        # Go to the start page
        first_tab.goto(url=START_PAGE_URL, timeout=REQUEST_TIMEOUT_MS)
    # The page did not load
    except PlaywrightError as error:
        # A dead Chrome must restart
        restart_browser_if_error_is_fatal(error)
        # Tell the user
        logger.warning(f"Could not open the start page: {error}")
    # Logged in without help from the user
    if try_automatic_login(tab=first_tab):
        # Search automatically
        results_tab = run_advanced_search(browser_context=browser_context, tab=first_tab)
        # The results grid is showing
        if results_tab is not None:
            # Log the success
            logger.info("Found the results grid")
            # Start scraping
            return results_tab
        # Explain
        logger.warning("Automatic search failed, switching to the manual steps")
    # The automatic log-in did not work
    else:
        # Explain
        logger.warning("Automatic log-in failed, switching to the manual steps")
    # The first prompt (manual fallback)
    prompt_message = START_INSTRUCTIONS
    # Until the grid is found
    while True:
        # Wait for the user
        wait_for_user_enter(tab=first_tab, message=prompt_message)
        # Look for the grid on every tab (the grid only exists after a log-in)
        results_tab = find_results_tab(browser_context=browser_context)
        # Found it
        if results_tab is not None:
            # Log the success
            logger.info("Found the results grid")
            return results_tab
        # Does any open tab show the logout link?
        logged_in = any(
            tab_shows_logout_link(tab=open_tab) for open_tab in browser_context.pages if not open_tab.is_closed()
        )
        # Ask for the right next step
        prompt_message = NO_RESULTS_REMINDER if logged_in else NOT_LOGGED_IN_REMINDER


# ===========================================================================
# WALKING THROUGH THE RESULTS PAGES
# ===========================================================================
# Read the current and total page numbers from the page label
def read_page_numbers(*, tab: Page) -> tuple[int | None, int | None]:
    # The label can be missing or slow
    try:
        # The label text
        label_text = tab.locator(selector=PAGE_LABEL_SELECTOR).text_content(timeout=10_000) or ""
    # The label could not be read
    except PlaywrightError as error:
        # A dead Chrome must restart
        restart_browser_if_error_is_fatal(error)
        # Report "unknown"
        return None, None
    # Find the page numbers
    match_found = PAGE_LABEL_PATTERN.search(string=label_text.replace(NON_BREAKING_SPACE, " "))
    # The text has an unexpected shape
    if not match_found:
        return None, None
    # The numbers as integers
    return int(match_found.group("current")), int(match_found.group("total"))


# Read the internal ID of the first document in the grid ('' if unknown)
def read_first_internal_id(*, tab: Page) -> str:
    # The page may be busy
    try:
        # Run the JavaScript
        return str(tab.evaluate(expression=FIRST_INTERNAL_ID_JAVASCRIPT) or "")
    # The page could not be inspected
    except PlaywrightError as error:
        # A dead Chrome must restart
        restart_browser_if_error_is_fatal(error)
        # Report "unknown"
        return ""


# Find the box where a page number can be typed
def find_page_number_box(*, tab: Page) -> Locator | None:
    # Every selector that may match the box
    for box_selector in PAGE_NUMBER_BOX_SELECTORS:
        # The page may be busy or closed
        try:
            # The first match of this selector
            candidate = tab.locator(selector=box_selector).first
            # It exists and a person could see it
            if candidate.count() > 0 and candidate.is_visible():
                # Report which selector matched
                logger.debug("Found the 'go to page' box with selector %s", box_selector)
                return candidate
        # The page cannot be inspected
        except PlaywrightError as error:
            # A dead Chrome must restart
            restart_browser_if_error_is_fatal(error)
    # No page box found
    return None


# Type a page number into the page box and press Enter; True if the grid shows that page now
def jump_to_results_page(*, browser_context: BrowserContext, tab: Page, target_page: int) -> bool:
    # A jump already failed in this run: use the Next button instead
    if run_state.page_jump_unavailable:
        return False
    # The box for the page number
    page_box = find_page_number_box(tab=tab)
    # Not found
    if page_box is None:
        # Do not search for it again and again
        run_state.page_jump_unavailable = True
        # Explain
        logger.warning(
            "Could not find the 'go to page' box on the results page, clicking Next instead "
            "(put the box's selector first in PAGE_NUMBER_BOX_SELECTORS)"
        )
        # Use the Next button instead
        return False
    # First document of the old page, to prove the grid was replaced
    previous_internal_id = read_first_internal_id(tab=tab)
    # Tell the user
    logger.info(f"Jumping to results page {target_page} by typing the page number")
    # Typing and waiting can fail
    try:
        # Focus the box like a person would
        page_box.click(timeout=PAGE_JUMP_TIMEOUT_MS)
        # Replace its content with the page number
        page_box.fill(str(target_page), timeout=PAGE_JUMP_TIMEOUT_MS)
        # Press Enter to go to that page
        page_box.press("Enter", timeout=PAGE_JUMP_TIMEOUT_MS)
        # Wait for the label and the rows to change
        tab.wait_for_function(
            expression=PAGE_CHANGE_CHECK_JAVASCRIPT,
            arg={"pageNumber": target_page, "previousInternalId": previous_internal_id},
            timeout=PAGE_JUMP_TIMEOUT_MS,
        )
    # Timeout or error during typing or waiting
    except PlaywrightError as error:
        # A dead Chrome must restart
        restart_browser_if_error_is_fatal(error)
        # Log it
        logger.warning(f"Jumping to results page {target_page} failed: {error}")
        # The jump may have worked, just slowly
        current_page_now, _ = read_page_numbers(tab=tab)
        # Already on the wanted page: success after all
        if current_page_now == target_page:
            return True
        # A logged-out website is a likely reason: restart Chrome and log in again (looks at the tab, no request)
        raise_if_results_tab_logged_out(tab=tab, label=f"jumping to results page {target_page}")
        # Click Next for the rest of this run
        run_state.page_jump_unavailable = True
        # The caller clicks Next instead
        return False
    # Tell the user
    logger.info(f"Jumped to results page {target_page}")
    # The grid shows the wanted page
    return True


# Click Next and wait until the label AND the grid changed (raises PagingFailedError after PAGING_MAXIMUM_ROUNDS failed rounds)
def go_to_next_results_page(*, browser_context: BrowserContext, tab: Page, next_page_number: int) -> None:
    # First document of the old page, to prove the grid was replaced
    previous_internal_id = read_first_internal_id(tab=tab)
    # Report the move
    logger.debug(
        "Going to results page %d (first document now: %r)", next_page_number, previous_internal_id
    )
    # Rounds of attempts
    round_number = 0
    # Until the page changed
    while True:
        # Count this round
        round_number += 1
        # The results tab was closed
        if tab.is_closed():
            raise BrowserLostError("the results tab was closed")
        # Chrome died
        raise_if_browser_unusable()
        # Several tries per round
        for attempt_number in range(1, RETRY_ATTEMPTS + 1):
            # Clicking or waiting can time out
            try:
                # Click the next page button
                tab.click(selector=NEXT_PAGE_BUTTON_SELECTOR, timeout=REQUEST_TIMEOUT_MS)
                # Wait for the label and the rows to change
                tab.wait_for_function(
                    expression=PAGE_CHANGE_CHECK_JAVASCRIPT,
                    arg={"pageNumber": next_page_number, "previousInternalId": previous_internal_id},
                    timeout=REQUEST_TIMEOUT_MS,
                )
                # Success
                return
            # Timeout or error during click or wait
            except PlaywrightError as error:
                # A dead Chrome must restart
                restart_browser_if_error_is_fatal(error)
                # Log it
                logger.warning(
                    f"Going to results page {next_page_number} failed "
                    f"(round {round_number}, try {attempt_number}/{RETRY_ATTEMPTS}): {error}"
                )
                # The click may have worked, just slowly
                current_page_now, _ = read_page_numbers(tab=tab)
                # Already on the wanted page: do NOT click again, or a page would be skipped
                if current_page_now == next_page_number:
                    return
                # A logged-out website is the most likely reason: restart Chrome and log in again (no request)
                raise_if_results_tab_logged_out(tab=tab, label=f"paging to results page {next_page_number}")
                # Short pause before the next try
                sleep_and_log(seconds=SECONDS_PER_RETRY_STEP * attempt_number)
        # Enough rounds: this will not fix itself without a restart
        if round_number >= PAGING_MAXIMUM_ROUNDS:
            # What went wrong
            reason = (
                f"paging to results page {next_page_number} failed in {round_number} rounds "
                "(did the site layout change?)"
            )
            # Remember it for the final summary
            record_problem(label=f"paging to results page {next_page_number}", url=SEARCH_PAGE_URL, reason=reason)
            # The main loop restarts Chrome (and stops only after several restarts without progress)
            raise PagingFailedError(reason)
        # Longer pauses between rounds
        wait_seconds = min(MAXIMUM_BACKOFF_SECONDS, SECONDS_PER_RETRY_STEP * 2**round_number)
        # Explain
        logger.error(
            f"Paging to results page {next_page_number} keeps failing "
            f"(round {round_number}/{PAGING_MAXIMUM_ROUNDS}); trying again in {wait_seconds}s"
        )
        # Wait before the next round
        sleep_and_log(seconds=wait_seconds)


# Read the documents of the current results page (a failed read is never returned as an empty page)
def read_results_page_records(*, browser_context: BrowserContext, tab: Page, page_number: int) -> list[DocumentRecord]:
    # Report the read
    logger.debug("Reading the documents of results page %d", page_number)
    # Several rounds
    for round_number in range(1, PAGING_MAXIMUM_ROUNDS + 1):
        # Several tries per round
        for attempt_number in range(1, RETRY_ATTEMPTS + 1):
            # The results tab was closed
            if tab.is_closed():
                raise BrowserLostError("the results tab was closed")
            # Chrome died
            raise_if_browser_unusable()
            # Reading and parsing can fail
            try:
                # The HTML of the grid
                grid_html = tab.locator(selector=RESULTS_GRID_SELECTOR).evaluate(expression=GET_OUTER_HTML_JAVASCRIPT)
                # Parse it into records
                return parse_results_grid(html_text=grid_html)
            # Browser or parsing problem
            except (PlaywrightError, etree.LxmlError, ValueError) as error:
                # A dead Chrome must restart
                restart_browser_if_error_is_fatal(error)
                # Log it
                logger.warning(
                    f"Reading results page {page_number} failed "
                    f"(round {round_number}, try {attempt_number}/{RETRY_ATTEMPTS}): {error}"
                )
                # Wait before trying again
                sleep_and_log(seconds=SECONDS_PER_RETRY_STEP * attempt_number)
        # A logged-out website empties the grid: restart Chrome and log in again (looks at the tab, no request)
        raise_if_results_tab_logged_out(tab=tab, label=f"reading results page {page_number}")
        # Another round follows
        if round_number < PAGING_MAXIMUM_ROUNDS:
            # Longer pauses between rounds
            wait_seconds = min(MAXIMUM_BACKOFF_SECONDS, SECONDS_PER_RETRY_STEP * 2**round_number)
            # Explain
            logger.error(f"Results page {page_number} could not be read; trying again in {wait_seconds}s")
            # Wait before the next round
            sleep_and_log(seconds=wait_seconds)
    # What went wrong
    reason = f"results page {page_number} could not be read in {PAGING_MAXIMUM_ROUNDS} rounds"
    # Remember it for the final summary
    record_problem(label=f"results page {page_number}", url=SEARCH_PAGE_URL, reason=reason)
    # The main loop restarts Chrome instead of skipping the page
    raise PagingFailedError(reason)


# Yield (page number, total pages, documents) for every results page, starting at the saved page if there is one
def iterate_results_pages(*, browser_context: BrowserContext, tab: Page) -> Iterator[tuple[int, int, list[DocumentRecord]]]:
    # The page the grid shows now
    current_page_number: int | None = None
    # The number of pages
    total_page_count: int | None = None
    # The page counter may need a moment
    for attempt_number in range(1, RETRY_ATTEMPTS + 1):
        # Read the first page numbers
        current_page_number, total_page_count = read_page_numbers(tab=tab)
        # Both numbers were read
        if current_page_number is not None and total_page_count is not None:
            break
        # Explain
        logger.error("Could not read 'Page X of Y' from the results page.")
        # Wait, then try again
        sleep_and_log(seconds=SECONDS_PER_RETRY_STEP * attempt_number)
    # The counter could never be read (no Enter prompt: this must work unattended)
    else:
        # The main loop restarts Chrome
        raise BrowserLostError("cannot read 'Page X of Y'; restarting Chrome and logging in again")
    # Log where we start
    logger.info(f"Starting at results page {current_page_number} of {total_page_count}")
    # The page where the last run stopped
    saved_page_number = load_saved_results_page()
    # Report the saved page
    logger.debug("Saved resume page: %s", saved_page_number)
    # An earlier run saved a page
    if saved_page_number is not None:
        # The saved page can be reached by paging forward
        if current_page_number < saved_page_number <= total_page_count:
            # Tell the user
            logger.info(
                f"Resuming at results page {saved_page_number} (saved by an earlier run), "
                f"paging forward from page {current_page_number}"
            )
            # One step: type the page number and press Enter
            if jump_to_results_page(browser_context=browser_context, tab=tab, target_page=saved_page_number):
                # The grid shows the saved page now
                current_page_number = saved_page_number
                # The site may have changed the page total
                _, jumped_total = read_page_numbers(tab=tab)
                # The total could be read
                if jumped_total is not None:
                    # Use the fresh total
                    total_page_count = jumped_total
            # Only runs when the jump was not possible: click Next until the saved page is reached
            while current_page_number < saved_page_number:
                # Click Next without reading any documents
                go_to_next_results_page(
                    browser_context=browser_context, tab=tab, next_page_number=current_page_number + 1
                )
                # Count the page
                current_page_number += 1
                # Now and then
                if current_page_number % PAGES_BETWEEN_SKIP_LOGS == 0:
                    # Show the progress
                    logger.info(f"Paging forward: results page {current_page_number} of {total_page_count}")
                # Wait briefly to be polite to the server
                polite_pause()
        # The saved page does not exist any more
        elif saved_page_number > total_page_count:
            # Explain
            logger.warning(
                f"The saved results page {saved_page_number} is beyond the last page ({total_page_count}), "
                f"starting at page {current_page_number}"
            )
            # Forget the useless page
            clear_saved_results_page()
    # Over all pages
    while True:
        # The documents of this page
        page_records = read_results_page_records(
            browser_context=browser_context, tab=tab, page_number=current_page_number
        )
        # Nothing was found on this page
        if not page_records:
            # Just log it and carry on
            logger.warning(f"Results page {current_page_number} contains no documents")
        # Hand this page to the caller
        yield current_page_number, total_page_count, page_records
        # The last page was reached
        if current_page_number >= total_page_count:
            # End the generator
            return
        # Navigate to the next page
        go_to_next_results_page(browser_context=browser_context, tab=tab, next_page_number=current_page_number + 1)
        # Count the page
        current_page_number += 1
        # The site may have changed the page total
        _, latest_total = read_page_numbers(tab=tab)
        # The total could be read
        if latest_total is not None:
            # Use the fresh total
            total_page_count = latest_total
        # Wait briefly to be polite to the server
        polite_pause()


# ===========================================================================
# PROBLEMS, TOTALS AND SUMMARIES
# ===========================================================================
# Count a problem and remember it in memory (safe to call from several threads)
def record_problem(*, label: str, url: str, reason: str) -> None:
    # Many error texts already start with the label, so remove it to avoid saying it twice
    reason = reason.removeprefix(f"{label}: ")
    # Create a timestamp
    timestamp = datetime.now().strftime(LOG_DATE_FORMAT)
    # The counter and the list are shared by all threads
    with state_lock:
        # Count the problem
        run_state.problem_count += 1
        # Remember whether this is the very first problem of the run
        is_first_problem = run_state.problem_count == 1
        # Remember it for the final summary
        run_state.recorded_problems.append(ProblemRecord(timestamp=timestamp, label=label, url=url, reason=reason))
    # Log the problem
    logger.error(f"{label}: FAILED - {reason}")
    # The very first problem of the run
    if is_first_problem:
        # Tell the user
        logger.info("First problem recorded; every problem is listed again at the end of the run")


# Build the one-line summary of the running totals
def build_totals_summary() -> str:
    # Seconds since the start
    elapsed_seconds = time.monotonic() - run_state.started_at_monotonic
    # Average speed (a plain count during the first minute)
    documents_per_minute = (
        run_state.documents_processed / (elapsed_seconds / 60) if elapsed_seconds >= 60 else float(run_state.documents_processed)
    )
    # Build the text
    return (
        f"{run_state.documents_processed} documents ({documents_per_minute:.1f}/min), "
        f"{run_state.documents_skipped} documents skipped (finished earlier), "
        f"{run_state.pdfs_saved} PDFs saved ({run_state.bytes_written / BYTES_PER_MEGABYTE:.1f} MB), "
        f"{run_state.pdfs_skipped} skipped, "
        f"{len(run_state.new_documents)} new IDs, {len(run_state.updated_documents)} known IDs with new files, "
        f"{run_state.documents_without_pdfs} without PDFs, {run_state.suspect_pdf_count} suspect, "
        f"{len(run_state.refused_file_labels)} refused, {run_state.problem_count} problems, "
        f"{run_state.documents_recovered} recovered, {run_state.browser_restarts} restarts, "
        f"elapsed {format_elapsed_time(total_seconds=elapsed_seconds)}"
    )


# Print the current running totals
def log_totals() -> None:
    logger.info(f"Totals: {build_totals_summary()}")


# Log a heading and then at most MAXIMUM_ITEMS_SHOWN lines (used by the refused and suspect summaries)
def log_limited_list(*, heading: str, items: list[str], overflow_hint: str) -> None:
    # Nothing to list
    if not items:
        return
    # The big picture
    logger.warning(heading)
    # One line per item, cut at the limit
    for item in items[:MAXIMUM_ITEMS_SHOWN]:
        logger.warning(f"  {item}")
    # Items not listed
    hidden_count = len(items) - MAXIMUM_ITEMS_SHOWN
    # The list was cut short
    if hidden_count > 0:
        # Say so
        logger.warning(f"  ... and {hidden_count} more ({overflow_hint})")


# List the problems at the end of the run
def log_problem_summary() -> None:
    # Nothing went wrong
    if not run_state.recorded_problems:
        return
    # The big picture
    logger.warning(
        f"{len(run_state.recorded_problems)} problems were recorded, "
        f"{len(run_state.failing_documents)} documents still have problems"
    )
    # The most recent problems, sorted by label
    shown_problems = sorted(run_state.recorded_problems[-MAXIMUM_ITEMS_SHOWN:], key=lambda problem: problem.label)
    # Print each problem
    for problem in shown_problems:
        logger.warning(f"  {problem.timestamp} | {problem.label} | {problem.reason} | {problem.url}")
    # Problems not listed
    hidden_count = len(run_state.recorded_problems) - len(shown_problems)
    # The list was cut short
    if hidden_count > 0:
        # Say so
        logger.warning(f"  ... and {hidden_count} older problems not shown")


# List the new IDs and the new files of known IDs found in this run
def log_change_summary() -> None:
    # The big picture
    logger.info(
        f"Changes in this run: {len(run_state.new_documents)} new IDs, "
        f"{len(run_state.updated_documents)} known IDs received new files"
    )
    # Each new ID
    for internal_id, public_id in list(run_state.new_documents.items())[:MAXIMUM_ITEMS_SHOWN]:
        logger.info(f"  new ID {internal_id} ({public_id})")
    # Each known ID with new files
    for internal_id, file_numbers in list(run_state.updated_documents.items())[:MAXIMUM_ITEMS_SHOWN]:
        logger.info(f"  known ID {internal_id}: new files {', '.join(file_numbers)}")


# ===========================================================================
# DOWNLOADING THE PDFS OF A DOCUMENT
# ===========================================================================
# Check that an already downloaded file is plausible
def is_saved_pdf_valid(*, file_path: Path) -> bool:
    # The file system can fail
    try:
        # A tiny file is broken
        if file_path.stat().st_size < MINIMUM_PDF_BYTES:
            return False
        # Open the file
        with file_path.open(mode="rb") as existing_file:
            # Read the beginning
            file_head = existing_file.read(PDF_SIGNATURE_SEARCH_BYTES)
        # A real PDF starts with the signature
        return PDF_SIGNATURE in file_head
    # The file cannot be read
    except OSError:
        # Treat it as broken
        return False


# Find saved files of this file number even if the rest of the name differs
def find_files_by_number(*, document_folder: Path, pdf_link: PdfLink) -> list[Path]:
    # The file number as it is written in file names
    number_part = sanitize_name_part(
        text=pdf_link["file_number"], maximum_length=FILE_NUMBER_MAXIMUM_LENGTH, fallback=FALLBACK_FILE_NUMBER
    )
    # The folder may not exist or be unreadable
    try:
        # Complete and incomplete-looking files with this number
        return sorted(
            [
                *document_folder.glob(f"*_{number_part}{PDF_FILE_EXTENSION}"),
                *document_folder.glob(f"*_{number_part}{SUSPECT_MARKER}{PDF_FILE_EXTENSION}"),
            ]
        )
    # The folder cannot be read
    except OSError:
        # Nothing found
        return []


# Skip a PDF that is already saved (no request is made for it); True if it should be skipped
def should_skip_existing_file(
    *, document_record: DocumentRecord, pdf_link: PdfLink, file_label: str, target_path: Path, suspect_path: Path
) -> bool:
    # Where the database says the file is
    recorded_path = find_recorded_file_path(
        internal_id=document_record["internal_id"], file_number=pdf_link["file_number"]
    )
    # The recorded path, the expected names, then any file in the folder with this file number
    for candidate_path in (
        recorded_path,
        target_path,
        suspect_path,
        *find_files_by_number(document_folder=target_path.parent, pdf_link=pdf_link),
    ):
        # Show every file that is considered
        logger.debug("%s: checking candidate %s (recorded path: %s)", file_label, candidate_path, recorded_path)
        # Nothing there
        if candidate_path is None or not candidate_path.exists():
            continue
        # The file is broken
        if not is_saved_pdf_valid(file_path=candidate_path):
            # Explain
            logger.warning(f"{file_label}: the existing file {candidate_path.name} looks broken, downloading it again")
            # Try the next candidate
            continue
        # Log the skip (a saved ".suspect" file is skipped too: the same URL is not requested twice)
        logger.info(f"{file_label}: already downloaded ({candidate_path.name}), skipping")
        # Count it
        add_to_counter(counter_name="pdfs_skipped")
        # The database does not know this file yet
        if recorded_path is None:
            # Add it
            record_file_found_on_disk(document_record=document_record, pdf_link=pdf_link, saved_path=candidate_path)
        # Skip
        return True
    # Nothing usable on disk: download it
    return False


# Record a disk problem while saving
def record_save_failure(*, file_label: str, target_path: Path, error: OSError) -> None:
    # Look at the operating system error number
    match error.errno:
        # The disk is full
        case errno.ENOSPC:
            # Remember it
            record_problem(label=file_label, url=str(target_path), reason="the disk was full (will be retried)")
            # Wait until space was freed instead of stopping
            wait_for_free_disk_space()
        # Any other file problem
        case _:
            # Remember it
            record_problem(label=file_label, url=str(target_path), reason=f"could not save the file: {error}")


# Update the totals and the database after a PDF was saved
def register_saved_pdf(
    *,
    document_record: DocumentRecord,
    pdf_link: PdfLink,
    file_label: str,
    final_path: Path,
    downloaded_pdf: DownloadedPdf,
    is_suspect: bool,
    document_is_new: bool,
) -> None:
    # The file looks incomplete
    if is_suspect:
        # Count it
        add_to_counter(counter_name="suspect_pdf_count")
        # Remember which file it is (the list is shared by all workers)
        with state_lock:
            # Add the path
            run_state.suspect_file_paths.append(str(final_path))
        # Warn
        logger.warning(f"{file_label}: no %%EOF end marker, the file may be incomplete; saved as {final_path.name}")
        # Remember it in the database
        record_suspect_retry(document_record=document_record, pdf_link=pdf_link)
    # The file looks complete
    else:
        # No suspect mark is needed any more
        clear_suspect_retry(document_record=document_record, pdf_link=pdf_link)
        # Remove an older incomplete-looking copy
        delete_file_quietly(file_path=build_suspect_path(target_path=final_path))
    # Log the success
    logger.info(f"{file_label}: SAVED -> {final_path} ({downloaded_pdf.size_bytes} bytes, sha256 {downloaded_pdf.sha256_hex[:12]})")
    # Count the file
    add_to_counter(counter_name="pdfs_saved")
    # Add to the byte counter
    add_to_counter(counter_name="bytes_written", amount=downloaded_pdf.size_bytes)
    # Remember it in the database
    record_saved_file(
        document_record=document_record,
        pdf_link=pdf_link,
        saved_path=final_path,
        downloaded_pdf=downloaded_pdf,
        is_suspect=is_suspect,
    )
    # Announce it if the ID was already known
    announce_new_file(document_record=document_record, pdf_link=pdf_link, document_is_new=document_is_new)


# Download and save one PDF; True if it worked (or needs no work)
def download_one_pdf(
    *,
    browser_context: BrowserContext,
    document_record: DocumentRecord,
    pdf_link: PdfLink,
    file_label: str,
    document_folder: Path,
    document_page_url: str,
    document_is_new: bool,
) -> bool:
    # Final path
    target_path = document_folder / build_file_name(document_folder=document_folder, pdf_link=pdf_link)
    # Path used if the file looks incomplete
    suspect_path = build_suspect_path(target_path=target_path)
    # Temporary file
    partial_path = target_path.with_name(target_path.name + PARTIAL_FILE_EXTENSION)
    # Report the target
    logger.debug("%s: target path is %s", file_label, target_path)
    # Already saved earlier: nothing to do, and no request was made
    if should_skip_existing_file(
        document_record=document_record,
        pdf_link=pdf_link,
        file_label=file_label,
        target_path=target_path,
        suspect_path=suspect_path,
    ):
        return True
    # The server refused this file recently: nothing to do, and no request was made
    if was_file_refused_recently(internal_id=document_record["internal_id"], file_number=pdf_link["file_number"]):
        # Log the skip
        logger.info(
            f"{file_label}: the server refused this file within the last {REFUSED_RECHECK_HOURS} hours, not asking again"
        )
        return True
    # Downloading and saving can fail
    try:
        # Let Chrome download the PDF (one request)
        downloaded_pdf = download_pdf_with_retries(
            browser_context=browser_context,
            url=pdf_link["download_url"],
            label=file_label,
            document_page_url=document_page_url,
            partial_path=partial_path,
        )
        # Complete PDFs normally end with %%EOF
        is_suspect = not downloaded_pdf.has_end_marker
        # Suspect files get a different name
        final_path = suspect_path if is_suspect else target_path
        # Rename the finished file in one atomic step
        downloaded_pdf.partial_path.replace(final_path)
    # Chrome is gone (or we were logged out)
    except BrowserLostError:
        # Let the main loop restart the session (this is not a problem of this file)
        raise
    # The server refused to send the file
    except RefusedDownloadError as error:
        # Remember it, so it is not requested again soon
        record_refused_file(document_record=document_record, pdf_link=pdf_link, reason=str(error))
        # Remember it for the final summary (the list is shared by all workers)
        with state_lock:
            # Add the label
            run_state.refused_file_labels.append(f"{document_record['internal_id']}/{pdf_link['file_number']}")
        # Tell the user
        logger.warning(str(error))
        # Not a problem of this run: it is listed in the summary
        return True
    # The download failed for good
    except PermanentFetchError as error:
        # Remember it
        record_problem(label=file_label, url=pdf_link["download_url"], reason=str(error))
        # This file failed
        return False
    # Saving to disk failed
    except OSError as error:
        # Remember it
        record_save_failure(file_label=file_label, target_path=target_path, error=error)
        # This file failed
        return False
    # Any other surprise must not stop the run either
    except Exception as error:
        # But a dead Chrome must restart instead of failing document after document
        restart_browser_if_error_is_fatal(error)
        # Remember it
        record_problem(label=file_label, url=pdf_link["download_url"], reason=f"unexpected error: {error!r}")
        # This file failed
        return False
    # Download and saving both worked
    else:
        # Update totals and database
        register_saved_pdf(
            document_record=document_record,
            pdf_link=pdf_link,
            file_label=file_label,
            final_path=final_path,
            downloaded_pdf=downloaded_pdf,
            is_suspect=is_suspect,
            document_is_new=document_is_new,
        )
        # This file worked
        return True
    # Always runs
    finally:
        # The temporary file must never stay behind
        delete_file_quietly(file_path=partial_path)
        # Do not waste time pausing when Chrome is about to restart
        if not run_state.browser_is_closed and not run_state.network_is_down:
            # Wait briefly between file downloads
            polite_pause()


# Let Chrome visit a document page and read its PDF links (None on failure)
def load_pdf_links(
    *, browser_context: BrowserContext, document_record: DocumentRecord, document_label: str
) -> tuple[str, list[PdfLink]] | None:
    # Address of the document page
    page_request_url = DOCUMENT_PAGE_URL_TEMPLATE.format(document_record["internal_id"])
    # Fetching can fail
    try:
        # Let Chrome visit and validate the page
        final_page_url, page_body = fetch_page_with_retries(
            browser_context=browser_context,
            url=page_request_url,
            label=f"{document_label} document page",
            validate_page=validate_document_page,
        )
    # Every attempt failed
    except PermanentFetchError as error:
        # Remember it
        record_problem(label=document_label, url=page_request_url, reason=str(error))
        # The document failed
        return None
    # HTML parsing can fail on odd pages
    try:
        # The final URL and the links
        return final_page_url, parse_document_page(
            html_text=decode_html_bytes(page_body=page_body), page_url=final_page_url
        )
    # Parsing problem
    except (etree.LxmlError, ValueError) as error:
        # Remember it
        record_problem(
            label=document_label, url=final_page_url, reason=f"could not parse the document page: {error}"
        )
        # The document failed
        return None


# Check one ID: read its page, download only the files not saved yet; True if everything worked
def process_document_files(
    *, browser_context: BrowserContext, document_record: DocumentRecord, document_label: str, count_document: bool
) -> bool:
    # Retries of a failed document are not counted again
    if count_document:
        # Count this document
        add_to_counter(counter_name="documents_processed")
    # The document page and its links (one request)
    loaded_page = load_pdf_links(
        browser_context=browser_context, document_record=document_record, document_label=document_label
    )
    # Loading failed: the document failed
    if loaded_page is None:
        return False
    # Unpack the result
    document_page_url, pdf_links = loaded_page
    # Internal document ID
    internal_id = document_record["internal_id"]
    # Did no earlier run see this ID? (read under the lock, because other workers announce IDs too)
    with state_lock:
        # Announced in this run already?
        announced_in_this_run = internal_id in run_state.new_documents
    # New if announced in this run, or unknown to the database
    document_is_new = announced_in_this_run or not is_document_known(internal_id=internal_id)
    # Report the verdict
    logger.debug("%s: document is new: %s, PDF links: %d", document_label, document_is_new, len(pdf_links))
    # A new ID
    if document_is_new:
        # Announce it
        announce_new_document(document_record=document_record)
    # The page lists no PDF files
    if not pdf_links:
        # Count it
        add_to_counter(counter_name="documents_without_pdfs")
        # Log it
        logger.warning(f"{document_label}: no PDF links found on the document page")
        # Remember that it was checked
        record_document_checked(document_record=document_record, pdf_count=0)
        # Not an error in itself
        return True
    # The folder of this document
    document_folder = PDF_FOLDER / build_folder_name(document_record=document_record)
    # Whether every file of this document worked
    document_ok = True
    # Every PDF listed on the page
    for file_position, pdf_link in enumerate(pdf_links, start=1):
        # Download it unless it is already saved
        file_ok = download_one_pdf(
            browser_context=browser_context,
            document_record=document_record,
            pdf_link=pdf_link,
            file_label=f"{document_label} file {file_position}/{len(pdf_links)}",
            document_folder=document_folder,
            document_page_url=document_page_url,
            document_is_new=document_is_new,
        )
        # One failure fails the document
        document_ok = document_ok and file_ok
    # Every file is saved
    if document_ok:
        # Remember that the ID was checked now (later runs skip it without any request)
        record_document_checked(document_record=document_record, pdf_count=len(pdf_links))
    # Report the result
    return document_ok


# Check one document and sort it into "done" or "still failing"
def process_document(
    *, browser_context: BrowserContext, document_record: DocumentRecord, document_label: str, log_suffix: str
) -> bool:
    # Internal document ID
    internal_id = document_record["internal_id"]
    # The label shown in the log (the stored label never gets suffixes)
    log_label = document_label + log_suffix
    # A retry has a suffix
    is_retry = bool(log_suffix)
    # Wait here while another worker is pausing for the circuit breaker
    with circuit_breaker_lock:
        # Nothing to do inside: only waiting for the lock matters
        pass
    # Remember when this document started
    started_at = time.monotonic()
    # Announce the document
    logger.info(f"--- START {log_label} (ident {internal_id}) ---")
    # NO error may escape from a single document (except "Chrome is gone")
    try:
        # Check the document
        document_ok = process_document_files(
            browser_context=browser_context,
            document_record=document_record,
            document_label=log_label,
            count_document=not is_retry,
        )
    # Chrome is gone (or we were logged out)
    except BrowserLostError:
        # Let the main loop restart the session (this is not a problem of this document)
        raise
    # Anything can go wrong
    except Exception as error:
        # A dead Chrome must restart instead of failing document after document
        restart_browser_if_error_is_fatal(error)
        # Remember it
        record_problem(
            label=log_label,
            url=DOCUMENT_PAGE_URL_TEMPLATE.format(internal_id),
            reason=f"unexpected error: {error!r}",
        )
        # This document failed
        document_ok = False
    # The dictionary is shared by all workers and read by the main thread, so it is only changed under the lock
    with state_lock:
        # Every file of the document worked
        if document_ok:
            # It failed earlier in this run
            if internal_id in run_state.failing_documents:
                # It is not a problem any more
                del run_state.failing_documents[internal_id]
                # Count the recovery
                add_to_counter(counter_name="documents_recovered")
        # Something went wrong
        else:
            # Earlier failure of the same document
            previous_failure = run_state.failing_documents.get(internal_id)
            # How often it failed now
            attempts = previous_failure.attempts + 1 if previous_failure is not None else 1
            # Remember it for the next retry round
            run_state.failing_documents[internal_id] = FailingDocument(
                record=document_record, label=document_label, attempts=attempts
            )
    # Report the result and the duration
    logger.info(f"--- END {log_label}: {'OK' if document_ok else 'FAILED'} in {time.monotonic() - started_at:.1f}s ---")
    # Pause after several failures in a row (no request is made to check the site)
    apply_circuit_breaker(document_ok=document_ok)
    # Report the result
    return document_ok


# ===========================================================================
# WORKER THREADS
# ===========================================================================
# One unit of work for a worker thread: check one document
@dataclass(frozen=True, slots=True, kw_only=True)
class DocumentTask:
    # The results-grid row of the document
    record: DocumentRecord
    # The original log label of the document
    label: str
    # Text added to the label in the log (empty for the first attempt, " (retry N)" for retries)
    log_suffix: str


# End the browser session for ALL threads; only the first error is kept
def record_session_abort(*, error: BaseException) -> None:
    # Only one thread at a time may change the error list
    with state_lock:
        # Keep the first (original) error only
        if not session_abort_errors:
            session_abort_errors.append(error)
    # Tell every thread to stop
    session_abort_event.set()


# Close the calling thread's download tab without ever complaining
def close_own_download_tab() -> None:
    # The tab of this thread
    own_tab = getattr(thread_local, "download_tab", None)
    # This thread never opened one
    if own_tab is None:
        return
    # Closing can fail
    try:
        # Close the tab
        own_tab.close()
    # The tab or the browser is already gone
    except PlaywrightError:
        pass


# Connect this worker to the one shared Chrome (a few tries, because the debugging port may need a moment)
def connect_to_shared_chrome(*, playwright_instance: Playwright) -> Any:
    # The address of Chrome's local debugging port
    endpoint_url = f"http://{REMOTE_DEBUGGING_HOST}:{run_state.remote_debugging_port}"
    # Several tries
    for attempt_number in range(1, WORKER_CONNECT_ATTEMPTS + 1):
        # Another thread already ended the session
        raise_if_browser_unusable()
        # Connecting can fail
        try:
            # Connect to the browser that the main thread launched (no new browser, no new window)
            return playwright_instance.chromium.connect_over_cdp(endpoint_url=endpoint_url)
        # Chrome does not answer (yet)
        except PlaywrightError as error:
            # The last try failed: give up so the session restarts
            if attempt_number == WORKER_CONNECT_ATTEMPTS:
                raise BrowserLostError(f"a worker could not connect to Chrome at {endpoint_url}: {error}") from error
            # Tell the user
            logger.warning(f"Worker could not connect to Chrome (try {attempt_number}/{WORKER_CONNECT_ATTEMPTS}): {error}")
            # Wait before the next try
            sleep_and_log(seconds=SECONDS_PER_RETRY_STEP * attempt_number)
    # Not reachable, but it keeps type checkers happy
    raise BrowserLostError("a worker could not connect to Chrome")


# The body of one worker thread: connect to the one Chrome, then check documents from the queue in an own tab
def run_worker_thread(*, work_queue: queue.Queue) -> None:
    # Any error that escapes ends the whole browser session
    try:
        # Playwright's sync API is bound to one thread, so every worker has its own Playwright connection
        with sync_playwright() as playwright_instance:
            # Connect to the one Chrome that the main thread launched (no new browser, no new window)
            browser = connect_to_shared_chrome(playwright_instance=playwright_instance)
            # Chrome has no usable window
            if not browser.contexts:
                raise BrowserLostError("the worker found no browser context in Chrome")
            # The default context is the logged-in profile of the main thread, so cookies are shared
            browser_context = browser.contexts[0]
            # Only when enabled: keep the session alive in this worker's pages too (off by default: repeated requests)
            if KEEP_ALIVE_ENABLED:
                # Add the keep-alive script (it guards against running twice)
                browser_context.add_init_script(script=KEEP_ALIVE_JAVASCRIPT)
            # Report it
            logger.info("Worker connected to the shared Chrome")
            # Work until told to stop
            try:
                # Loop over the queued documents
                while True:
                    # Another thread ended the session
                    if session_abort_event.is_set():
                        return
                    # Wait a short moment for the next task
                    try:
                        # Take the next task
                        task = work_queue.get(timeout=QUEUE_POLL_SECONDS)
                    # Nothing queued yet
                    except queue.Empty:
                        # Look again (and check the abort flag again)
                        continue
                    # Every task taken from the queue must be marked as done, even when it fails
                    try:
                        # None is the signal to stop
                        if task is None:
                            return
                        # Check the document in this worker's own tab
                        process_document(
                            browser_context=browser_context,
                            document_record=task.record,
                            document_label=task.label,
                            log_suffix=task.log_suffix,
                        )
                        # Print the totals now and then
                        if run_state.documents_processed % PROGRESS_EVERY_N_DOCUMENTS == 0:
                            log_totals()
                    # The queue counts finished tasks, so the main thread knows when a page is done
                    finally:
                        work_queue.task_done()
            # The tab belongs to this worker, so it is closed when the worker ends
            finally:
                close_own_download_tab()
    # Chrome is gone, the network is down, or the connection failed: end the session for everybody
    except BaseException as error:
        # The main thread asked the workers to stop normally, so a late error must not restart the whole run
        if worker_shutdown_event.is_set() and not session_abort_event.is_set():
            # Only log it
            logger.debug("Ignoring an error during the normal worker shutdown: %r", error)
        # A real problem while working
        else:
            # Remember the original error and stop all threads
            record_session_abort(error=error)


# Start all worker threads
def start_worker_threads(*, work_queue: queue.Queue) -> list[threading.Thread]:
    # The started threads
    worker_threads: list[threading.Thread] = []
    # One thread per worker
    for worker_number in range(1, WORKER_COUNT + 1):
        # Daemon threads never block the exit of the program
        worker_thread = threading.Thread(
            target=run_worker_thread, kwargs={"work_queue": work_queue}, name=f"worker-{worker_number}", daemon=True
        )
        # Start it
        worker_thread.start()
        # Remember it
        worker_threads.append(worker_thread)
    # Tell the user
    logger.info(f"Started {WORKER_COUNT} worker threads (they share the one Chrome and use their own tabs)")
    # Hand the threads back
    return worker_threads


# Tell the workers to stop and wait until all of them have ended
def stop_worker_threads(*, work_queue: queue.Queue, worker_threads: list[threading.Thread]) -> None:
    # A normal shutdown (no abort yet): errors that happen while the workers disconnect are not real problems
    if not session_abort_event.is_set():
        # Remember that this is a normal shutdown
        worker_shutdown_event.set()
    # One stop signal per worker
    for _ in worker_threads:
        # The queue may be full when the session is aborted
        try:
            # None tells a worker to stop
            work_queue.put_nowait(None)
        # Aborted workers stop by themselves because of the abort flag
        except queue.Full:
            pass
    # Wait for every worker (a worker finishes the file it is downloading first)
    for worker_thread in worker_threads:
        # Wait until it ended
        worker_thread.join()


# Put one task in the queue (waits while the queue is full, keeping the browser events flowing)
def enqueue_task(*, work_queue: queue.Queue, results_tab: Page, task: DocumentTask) -> None:
    # Until the task fits
    while True:
        # Stop if Chrome died, the network went down or a worker ended the session
        raise_if_browser_unusable()
        # The queue may be full
        try:
            # Try to add the task
            work_queue.put(task, timeout=QUEUE_POLL_SECONDS)
            # Done
            return
        # The queue is still full
        except queue.Full:
            # Let the main thread's Playwright connection process events (it also logs the worker tabs)
            let_browser_process_events(tab=results_tab)


# Wait until every queued task is finished
def wait_for_queue_to_finish(*, work_queue: queue.Queue, results_tab: Page) -> None:
    # unfinished_tasks counts queued tasks plus tasks that workers are still processing
    while work_queue.unfinished_tasks > 0:
        # Stop if Chrome died, the network went down or a worker ended the session
        raise_if_browser_unusable()
        # Let the main thread's Playwright connection process events
        let_browser_process_events(tab=results_tab)


# Give failed documents another chance (every few pages, and once more at the end)
def retry_failing_documents(
    *, work_queue: queue.Queue, results_tab: Page, rounds: int, is_final: bool
) -> None:
    # A few rounds
    for round_number in range(1, rounds + 1):
        # During the run, documents that failed too often are left for the final round
        with state_lock:
            waiting_documents = [
                failing_document
                for failing_document in run_state.failing_documents.values()
                if is_final or failing_document.attempts < MAXIMUM_DOCUMENT_ATTEMPTS
            ]
        # Nothing left to retry
        if not waiting_documents:
            return
        # Announce the round
        logger.info(f"=== Retry round {round_number}/{rounds}: {len(waiting_documents)} documents ===")
        # Hand every failed document to the workers
        for failing_document in waiting_documents:
            # The suffix is only for logging
            enqueue_task(
                work_queue=work_queue,
                results_tab=results_tab,
                task=DocumentTask(
                    record=failing_document.record,
                    label=failing_document.label,
                    log_suffix=f" (retry {round_number})",
                ),
            )
        # Wait until the workers finished the whole round
        wait_for_queue_to_finish(work_queue=work_queue, results_tab=results_tab)
    # Some documents still fail at the very end
    if is_final and run_state.failing_documents:
        # Say so
        logger.warning(f"{len(run_state.failing_documents)} documents still fail after {rounds} retry rounds")


# Hand every document of one results page to the workers and wait until the whole page is finished
def process_results_page(
    *,
    work_queue: queue.Queue,
    results_tab: Page,
    page_number: int,
    page_records: list[DocumentRecord],
    seen_internal_ids: set[str],
) -> None:
    # Every document of the page
    for position, document_record in enumerate(page_records, start=1):
        # The results tab disappeared
        if results_tab.is_closed():
            raise BrowserLostError("the results tab was closed")
        # Internal document ID
        internal_id = document_record["internal_id"]
        # Already queued in this run (no second request)
        if internal_id in seen_internal_ids:
            # Log the skip
            logger.info(f"Document {internal_id} already checked in this run, skipping")
            continue
        # Remember this document
        seen_internal_ids.add(internal_id)
        # An earlier run finished this document completely: its page is not opened again (no URL twice)
        if not RECHECK_KNOWN_DOCUMENTS and was_document_checked_before(internal_id=internal_id):
            # Log the skip
            logger.info(f"Document {internal_id} was finished in an earlier run, not opening it again")
            # Count it
            add_to_counter(counter_name="documents_skipped")
            # Next document
            continue
        # Hand it to the workers
        enqueue_task(
            work_queue=work_queue,
            results_tab=results_tab,
            task=DocumentTask(
                record=document_record,
                label=f"[p{page_number} {position}/{len(page_records)}] {document_record['public_id'] or internal_id}",
                log_suffix="",
            ),
        )
    # The saved resume page is only correct when the whole page is finished before the next page starts
    wait_for_queue_to_finish(work_queue=work_queue, results_tab=results_tab)


# Control the whole downloading process of one browser session (this runs in the main thread)
def download_all_documents(*, browser_context: BrowserContext) -> None:
    # Log in, search and wait until the results grid is shown
    results_tab = open_results_grid(browser_context=browser_context)
    # The queue between the main thread (producer) and the workers (consumers)
    work_queue: queue.Queue = queue.Queue(maxsize=WORKER_COUNT * QUEUE_SLOTS_PER_WORKER)
    # Documents seen in this session (a document on two pages is done once)
    seen_internal_ids: set[str] = set()
    # Start the workers only now, when the log-in is done and the profile holds a valid session
    worker_threads = start_worker_threads(work_queue=work_queue)
    # Whatever happens, the workers must be stopped and joined
    try:
        # Every results page
        for page_number, total_page_count, page_records in iterate_results_pages(
            browser_context=browser_context, tab=results_tab
        ):
            # Log the page
            logger.info(f"=== Results page {page_number}/{total_page_count}: {len(page_records)} documents ===")
            # Remember this page, so a restart or crash continues here
            save_current_results_page(page_number=page_number, total_pages=total_page_count)
            # A new furthest page resets the restart counters
            record_results_page_progress(page_number=page_number)
            # Let the workers download its documents and wait until they are all finished
            process_results_page(
                work_queue=work_queue,
                results_tab=results_tab,
                page_number=page_number,
                page_records=page_records,
                seen_internal_ids=seen_internal_ids,
            )
            # Time to retry what failed on earlier pages
            if page_number % RETRY_FAILED_EVERY_N_PAGES == 0:
                # Retry now instead of waiting for the very end
                retry_failing_documents(work_queue=work_queue, results_tab=results_tab, rounds=1, is_final=False)
        # Give every failed document another chance
        retry_failing_documents(
            work_queue=work_queue, results_tab=results_tab, rounds=FINAL_RETRY_ROUNDS, is_final=True
        )
        # The whole search was finished, so the next run starts at page 1 again
        clear_saved_results_page()
        # Tell the user
        logger.info("Finished all results pages")
    # Any error of the main thread (including Ctrl+C) ends the session for the workers too
    except BaseException as main_error:
        # Keeps the FIRST error: a worker's original error wins over the main thread's follow-up error
        record_session_abort(error=main_error)
    # Always runs
    finally:
        # Stop the workers and wait until every one has ended
        stop_worker_threads(work_queue=work_queue, worker_threads=worker_threads)
    # Something ended the session: raise the original error here, so the main loop handles it
    if session_abort_errors:
        raise session_abort_errors[0]


# ===========================================================================
# MAIN PROGRAM
# ===========================================================================
# Run one complete browser session; True when the whole run finished normally
def run_browser_session(*, playwright_instance: Playwright) -> bool:
    # A new session starts without any earlier abort
    session_abort_event.clear()
    # Forget the errors of the previous session
    session_abort_errors.clear()
    # A new session is not shutting down
    worker_shutdown_event.clear()
    # The configured browser
    browser_context = launch_browser(playwright_instance=playwright_instance)
    # The browser must always be closed
    try:
        # Start the main process
        download_all_documents(browser_context=browser_context)
        # The whole run finished normally
        return True
    # Runs no matter what
    finally:
        # Log the shutdown
        logger.info("Closing the browser")
        # Late close events of this context must not fake a crash of the next session
        run_state.active_browser_context = None
        # Closing can fail
        try:
            # Close the browser (this also closes the worker tabs)
            browser_context.close()
        # The browser is already gone
        except PlaywrightError:
            pass


# Count a restart and say how long to wait before the next session
def count_restart_and_get_wait_seconds() -> int:
    # Count the restart for the summary
    run_state.browser_restarts += 1
    # Count it for the growing wait
    run_state.restarts_without_progress += 1
    # Wait longer after every restart without progress
    return min(MAXIMUM_RESTART_WAIT_SECONDS, RESTART_WAIT_SECONDS * run_state.restarts_without_progress)


# Keep starting browser sessions until one finishes
def run_until_finished() -> None:
    # Becomes True when the whole run completed
    run_finished = False
    # Until a session finishes
    while not run_finished:
        # Chrome only starts after ping gets replies
        wait_until_internet_works()
        # A new outage is noticed from now on
        run_state.network_is_down = False
        # From now on the watchdog stops Chrome if ping stays silent
        run_state.network_watch_enabled = ping_command_exists()
        # One session
        try:
            # Announce every browser session
            logger.info(f"=== Starting browser session #{run_state.browser_restarts + 1} ===")
            # Start Playwright
            with sync_playwright() as playwright_instance:
                # Run one session
                run_finished = run_browser_session(playwright_instance=playwright_instance)
        # Paging or reading a results page failed for good
        except PagingFailedError as error:
            # Count it
            run_state.paging_failures_without_progress += 1
            # Restarting did not help several times in a row
            if run_state.paging_failures_without_progress > MAXIMUM_PAGING_RESTARTS:
                # Say clearly why the run ends
                logger.error(
                    f"STOPPING: {error}. Restarting Chrome {MAXIMUM_PAGING_RESTARTS} times did not help. "
                    "Fix the problem, then run again; finished documents and files are skipped."
                )
                # Ring the terminal bell
                ring_terminal_bell()
                # Remember it for the exit code
                run_state.stopped_early = True
                # End the run
                return
            # How long to wait
            wait_seconds = count_restart_and_get_wait_seconds()
            # Explain
            logger.warning(
                f"{error}: restarting Chrome and logging in again in {wait_seconds}s "
                "(finished documents and files are skipped)"
            )
            # Wait before the next session
            sleep_and_log(seconds=wait_seconds)
        # The internet went away during the run
        except NetworkDownError as error:
            # Explain (the waiting for ping happens at the top of the loop)
            logger.warning(
                f"{error}: Chrome was closed. It starts again when ping gets replies and the run continues "
                "at the saved results page (finished documents and files are skipped)"
            )
            # Short pause, so a dead website cannot cause a restart loop even when ping works
            sleep_and_log(seconds=count_restart_and_get_wait_seconds())
        # Chrome died, a tab crashed or the session ended (logout)
        except BrowserLostError as error:
            # How long to wait
            wait_seconds = count_restart_and_get_wait_seconds()
            # Explain
            logger.warning(
                f"{error}: restarting Chrome with an automatic log-in in {wait_seconds}s "
                "(finished documents and files are skipped)"
            )
            # Wait before the next session
            sleep_and_log(seconds=wait_seconds)
        # Any other crash of the session (browser closed, Chromium missing, ...)
        except Exception:
            # How long to wait
            wait_seconds = count_restart_and_get_wait_seconds()
            # Log the traceback
            logger.exception(
                f"The session crashed; restarting in {wait_seconds}s (finished documents and files are skipped)"
            )
            # Wait before the next session
            sleep_and_log(seconds=wait_seconds)
        # Runs after every session
        finally:
            # No Chrome is running between sessions, so the watchdog must not stop anything
            run_state.network_watch_enabled = False


# The entry point; returns the exit code
def main() -> int:
    # Start logging first, so everything below is recorded
    setup_logging()
    # Report the log level
    logger.info(f"Logging started at level {logging.getLevelName(LOG_LEVEL)}")
    # Assume success until something goes wrong
    exit_code = 0
    # The only way to stop the script is Ctrl+C
    try:
        # Check terminal, write access and disk space
        run_preflight_checks()
        # Tell what earlier runs already know
        log_database_overview()
        # Watch the internet in the background
        start_network_watchdog_thread()
        # Run until everything is done
        run_until_finished()
    # The user pressed Ctrl+C
    except KeyboardInterrupt:
        # Log it
        logger.warning("Stopped by you (Ctrl+C). Run again to continue; finished documents and files are skipped.")
        # The conventional exit code for Ctrl+C
        exit_code = 130
    # Runs before exiting
    finally:
        # Stop the network watchdog
        watchdog_stop_event.set()
        # Stop the background ping
        stop_all_ping_processes()
        # Close the local database
        close_database_connection()
        # Print the final totals
        log_totals()
        # List the new IDs and the new files of known IDs
        log_change_summary()
        # List the PDFs that look incomplete
        log_limited_list(
            heading=(
                f"{len(run_state.suspect_file_paths)} PDFs look incomplete (no %%EOF marker); "
                f"they were saved with '{SUSPECT_MARKER}' in the name:"
            ),
            items=run_state.suspect_file_paths,
            overflow_hint=f"see {STATE_DATABASE_FILE}",
        )
        # List the files the server refused
        log_limited_list(
            heading=(
                f"{len(run_state.refused_file_labels)} files were refused by the server "
                f"(not requested again for {REFUSED_RECHECK_HOURS} hours):"
            ),
            items=run_state.refused_file_labels,
            overflow_hint=f"see the 'refused' table in {STATE_DATABASE_FILE}",
        )
        # List the problems
        log_problem_summary()
        # Some documents still have problems, or the run stopped before the last results page
        if run_state.failing_documents or run_state.stopped_early:
            # Make sure the exit code reports it
            exit_code = exit_code or 1
    # Hand the exit code back
    return exit_code


# Only when the script is run directly
if __name__ == "__main__":
    # Run main and pass its exit code to the operating system
    sys.exit(main())

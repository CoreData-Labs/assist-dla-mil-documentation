# ASSIST (assist.dla.mil) PDF downloader - never-stop version: no arguments, every error is logged and skipped.
# Everything (pages, login checks and PDF downloads) goes through Chrome, so only the browser has to be logged in.
# Optional: ASSIST_LOG_FILE (rotating log file) and ASSIST_ALERT_WEBHOOK (message when the script waits for you).
# Every ID is checked again on every run (new PDFs can appear later); files that are already saved are never requested again.
# Requires Python 3.13 or newer.

import errno  # Recognise the "disk is full" operating system error
import hashlib  # Fingerprint every saved PDF
import json  # Progress file, manifest and webhook message
import logging  # Console logging
import logging.handlers  # Rotating log file
import os  # Environment variables and fsync
import queue  # Pass typed lines from the keyboard thread to the main thread
import random  # Jitter for the pauses between requests
import re  # Text pattern matching
import shutil  # Measure the free disk space
import sqlite3  # Local database of everything seen and saved in earlier runs
import sys  # Standard input and output
import threading  # Keyboard reader, heartbeat and webhook threads
import time  # Pauses and timing
import urllib.request  # Post the optional webhook alert
from collections.abc import (
    Callable,
    Iterator,
)  # Type hints for callbacks and generators
from dataclasses import dataclass, field  # Data holders
from datetime import datetime, timedelta  # Date formatting and time differences
from email.utils import parsedate_to_datetime  # Parse HTTP dates (Retry-After header)
from pathlib import Path  # Cross-platform file paths
from typing import Any, TypedDict  # Type hints
from urllib.parse import parse_qs, urljoin, urlparse  # URL parsing

from lxml import etree  # Catch lxml parsing errors precisely
from lxml import html as lxml_html  # Fast HTML parsing
from playwright.sync_api import (
    BrowserContext,
    Dialog,
    Download,
    Page,
    Playwright,
    Response,
)  # Playwright types
from playwright.sync_api import (
    Error as PlaywrightError,
)  # Network errors, timeouts, closed browser
from playwright.sync_api import sync_playwright  # Controls the Chromium browser

# ===========================================================================
# TYPES
# ===========================================================================
type AttemptFunction = Callable[
    ..., Any
]  # One attempt of a retried operation (called with attempt_label=...)
type BodyValidator = Callable[
    ..., None
]  # Checks a page (called with response=..., response_body=...)


class DocumentRecord(TypedDict):  # One row of the results grid
    ident: str  # Internal document ID (used in the document page URL)
    doc_id: str  # Public document ID (used as the folder name)


class PdfLink(TypedDict):  # One PDF found on a document page
    url: str  # Absolute download URL
    dmxid: str  # File number taken from the URL
    description: str  # Description text from the document page
    date: str  # Date text from the document page


@dataclass(frozen=True, slots=True, kw_only=True)  # Immutable record of one problem
class FailureRecord:  # One recorded problem
    timestamp: str  # When it happened
    label: str  # Which document or file it belongs to
    url: str  # The address (or path) involved
    reason: str  # What went wrong


@dataclass(
    frozen=True, slots=True, kw_only=True
)  # Immutable record of a document that is still failing
class FailedDocument:  # A document that will be retried
    record: DocumentRecord  # The grid row of the document
    label: str  # The ORIGINAL log label (retry suffixes are added only when logging)
    page_number: int  # The results page the document was found on


@dataclass(
    frozen=True, slots=True, kw_only=True
)  # Immutable result of one finished PDF download
class PdfDownload:  # A PDF that Chrome saved to a temporary file
    size: int  # Number of bytes on disk
    sha256: str  # Full SHA-256 of the file
    has_end_marker: bool  # True if the file ends with %%EOF (looks complete)
    partial_path: Path  # The temporary file that holds the data


# ===========================================================================
# SETTINGS
# ===========================================================================
SECONDS_BETWEEN_REQUESTS = 0.5  # Normal wait in seconds between web requests
MAXIMUM_DELAY_SECONDS = 10  # The adaptive delay never grows beyond this
JITTER_MINIMUM_FACTOR = (
    0.7  # Every pause is multiplied by a random factor of at least this
)
JITTER_MAXIMUM_FACTOR = 1.3  # ... and at most this
SHOW_BROWSER_WINDOW = True  # True makes the browser visible to the user
MINIMUM_FREE_DISK_BYTES = (
    500 * 1024 * 1024
)  # Pause while less than 500 MB of disk space is free
MINIMUM_PDF_BYTES = 1  # Any "PDF" smaller than this is treated as broken
MAXIMUM_RETRY_AFTER_SECONDS = (
    300  # Never wait longer than 5 minutes because a server asked us to slow down
)
DISK_WAIT_SECONDS = 30  # Wait between free-disk-space checks while the disk is too full
RESTART_WAIT_SECONDS = 10  # Base wait before the browser session restarts after a crash
MAXIMUM_RESTART_WAIT_SECONDS = 300  # The restart wait never grows beyond 5 minutes
MAXIMUM_PAGING_BACKOFF_SECONDS = (
    300  # Waits between paging attempts and breaker pauses never grow beyond 5 minutes
)
FINAL_RETRY_ROUNDS = (
    1  # Extra rounds spent on documents that failed (every retry costs requests)
)
MAXIMUM_RELOGINS_PER_REQUEST = (
    3  # How many times one download may ask you to log in again
)
CIRCUIT_BREAKER_FAILURES = 5  # This many failed documents in a row pauses the run
CIRCUIT_BREAKER_BASE_SECONDS = (
    30  # First breaker pause (doubles with every trip in a row)
)
NO_PDF_STREAK_BEFORE_ALERT = (
    25  # This many documents in a row without PDF links pauses the run
)
NO_PDF_STREAK_GROWTH_FACTOR = (
    4  # After you confirmed a streak, the next alert needs this many times more
)
EMPTY_PAGES_BEFORE_ALERT = (
    2  # This many results pages in a row without documents pauses the run
)
PROMPT_REPEAT_SECONDS = (
    600  # A waiting prompt is shown (and signalled) again every 10 minutes
)
HEARTBEAT_SECONDS = 300  # Print a "still running" line every 5 minutes
REFUSED_RECHECK_HOURS = (
    24  # A file the server refused is not requested again for this many hours
)
MAXIMUM_FAILURES_SHOWN = 100  # How many problems the final summary lists
BYTES_PER_MEGABYTE = 1024 * 1024  # Number of bytes in one megabyte
RETRY_ATTEMPTS = 3  # Number of times to retry a failed operation
SECONDS_PER_RETRY_STEP = 2  # Base backoff time in seconds for retries
PROGRESS_EVERY_N_DOCUMENTS = 25  # Frequency of progress totals
REQUEST_TIMEOUT_MS = 120_000  # Network timeout in milliseconds
KEYBOARD_POLL_MS = 300  # Polling interval for keyboard input in milliseconds
CLOSED_INPUT_WAIT_SECONDS = (
    5  # If terminal input is closed, prompts continue by themselves after this long
)
LOG_FILE_ENV_NAME = (
    "ASSIST_LOG_FILE"  # Environment variable that switches on the log file
)
LOG_FILE_MAXIMUM_BYTES = 10 * 1024 * 1024  # Rotate the log file after 10 MB
LOG_FILE_BACKUP_COUNT = 5  # Rotated log files to keep
LOG_LINE_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"  # Layout of a log line
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"  # Layout of the time in a log line
ALERT_WEBHOOK_ENV_NAME = (
    "ASSIST_ALERT_WEBHOOK"  # Environment variable with the optional webhook URL
)
WEBHOOK_TIMEOUT_SECONDS = 10  # Give up on a webhook call after this long
HASH_CHUNK_BYTES = 1024 * 1024  # Piece size when hashing a saved PDF
MAXIMUM_NON_PDF_BYTES = (
    2 * 1024 * 1024
)  # How much of a "not a PDF" file is read to recognise a login page
MAXIMUM_WINDOWS_PATH_LENGTH = (
    240  # On Windows, file paths are kept below this (the hard limit is 260)
)
MINIMUM_DESCRIPTION_LENGTH = (
    10  # The description part of a file name is never shortened below this
)

# ===========================================================================
# MESSAGES
# ===========================================================================
START_INSTRUCTIONS = (  # Start instructions shown in the terminal
    "\n>> In the browser window:\n"  # Header
    "   1. Log in\n"  # Step 1
    "   2. Go to the search page and select Advanced Search\n"  # Step 2
    "   3. Click Search (leave the filters empty to get EVERYTHING)\n"  # Step 3
    "   4. Stay on results page 1 (every ID is checked again on every run, so new IDs and new files are found)\n"  # Step 4
    "   Then press Enter here..."  # Prompt
)  # End of the start instructions
NOT_LOGGED_IN_REMINDER = "\n   You do not seem to be logged in yet. Log in, then press Enter here..."  # Not logged in
NO_RESULTS_REMINDER = (  # Shown when the results grid is missing
    "\n   I cannot see the results grid. Select Advanced Search, click Search,\n"  # Explain what to do
    "   wait for the results to appear, then press Enter here..."  # Prompt
)  # End of the reminder
LOGIN_TAB_MESSAGE = "\n>> Log in in the new browser tab, then press Enter here..."  # Shown when a new log-in is needed
LAYOUT_CHECK_MESSAGE = (  # Shown when the website may have changed its layout
    "\n>> Possible website layout change: {problem}\n"  # Describe the suspicion
    "   Look at the browser window. If everything is fine, press Enter here to continue..."  # Prompt
)  # End of the message

# ===========================================================================
# WEBSITE DETAILS
# ===========================================================================
BASE_URL = "https://assist.dla.mil"  # Base URL of the website
START_PAGE_URL = BASE_URL + "/online/start/"  # Log-in / start page
DETAIL_PAGE_URL_TEMPLATE = (
    BASE_URL + "/online/doc_analysis/doc_info_general.cfm?ident_number={}"
)  # Document page URL
LOGIN_CHECK_IDENT = (
    2  # A known document ID used to verify that the session is logged in
)
PDF_FOLDER = Path("PDFs")  # Folder where PDFs are saved
OUTPUT_FOLDER = Path("assist_data")  # Folder for script data
BROWSER_PROFILE_FOLDER = (
    OUTPUT_FOLDER / "browser_profile"
)  # Persistent browser profile (keeps you logged in)
STATE_DATABASE_FILE = (
    OUTPUT_FOLDER / "state.sqlite3"
)  # Local database: every ID and every file seen so far
DATABASE_SCHEMA = (  # Creates the tables on first use
    "CREATE TABLE IF NOT EXISTS documents ("  # IDs seen on the website
    "ident TEXT PRIMARY KEY, doc_id TEXT NOT NULL, first_seen TEXT NOT NULL, "  # Internal ID, public ID, first sight
    "last_checked TEXT NOT NULL, pdf_count INTEGER NOT NULL);"  # Last check and number of PDF links then
    "CREATE TABLE IF NOT EXISTS files ("  # PDFs saved
    "ident TEXT NOT NULL, dmxid TEXT NOT NULL, url TEXT NOT NULL, path TEXT NOT NULL, "  # Which document, which file, where
    "bytes INTEGER NOT NULL, sha256 TEXT NOT NULL, suspect INTEGER NOT NULL, saved_at TEXT NOT NULL, "  # Size, hash, state, time
    "PRIMARY KEY (ident, dmxid));"  # One row per file of a document
    "CREATE TABLE IF NOT EXISTS refused ("  # Files the server refused to send
    "ident TEXT NOT NULL, dmxid TEXT NOT NULL, url TEXT NOT NULL, refused_at TEXT NOT NULL, reason TEXT NOT NULL, "  # What and when
    "PRIMARY KEY (ident, dmxid));"  # One row per refused file
)  # End of the schema
PDF_FILE_EXTENSION = ".pdf"  # Extension of downloaded files
PARTIAL_FILE_EXTENSION = ".part"  # Extension of a file that is still being written
SUSPECT_MARKER = (
    ".suspect"  # Inserted before ".pdf" for files that still lack the end marker
)

# ===========================================================================
# FILE NAMES
# ===========================================================================
SAFE_NAME_PATTERN = re.compile(
    pattern=r"[^A-Za-z0-9.\-]+"
)  # Characters that are not allowed in names
NAME_FIELD_SEPARATOR = "_"  # Separates the parts of a file name
NAME_REPLACEMENT_CHARACTER = "_"  # Replaces invalid characters
NAME_EDGE_CHARACTERS = "_."  # Characters that must not start or end a name
RESERVED_WINDOWS_NAMES = frozenset(  # Names that Windows refuses for files and folders
    {"CON", "PRN", "AUX", "NUL"}  # Device names
    | {f"COM{number}" for number in range(1, 10)}  # Serial ports
    | {f"LPT{number}" for number in range(1, 10)}  # Parallel ports
)  # End of the reserved names
FOLDER_NAME_MAXIMUM_LENGTH = 60  # Maximum length of a folder name
DESCRIPTION_MAXIMUM_LENGTH = 50  # Maximum length of the description part of a file name
FILE_NUMBER_MAXIMUM_LENGTH = 20  # Maximum length of the file number part of a file name
FALLBACK_FOLDER_PREFIX = "ident_"  # Folder name prefix if the document ID is missing
FALLBACK_DESCRIPTION = "document"  # Description if it is missing
FALLBACK_DATE_TEXT = "no-date"  # Date text if it is missing or unparseable
FALLBACK_FILE_NUMBER = "no-number"  # File number if it is missing
DATE_INPUT_FORMATS = (
    "%d-%b-%Y",
    "%d-%B-%Y",
    "%m/%d/%Y",
    "%Y-%m-%d",
)  # Date layouts tried in turn
DATE_OUTPUT_FORMAT = "%Y-%m-%d"  # Date layout used in file names

# ===========================================================================
# PAGE ELEMENTS, JAVASCRIPT AND PATTERNS
# ===========================================================================
DIALOG_EVENT_NAME = "dialog"  # Playwright dialog event
RESULTS_GRID_SELECTOR = "#GV"  # Search results grid
PAGE_LABEL_SELECTOR = "#lblPageCount"  # Page counter label
NEXT_PAGE_BUTTON_SELECTOR = "#btnNext"  # Next page button
GET_OUTER_HTML_JAVASCRIPT = (
    "element => element.outerHTML"  # Extract the HTML of an element
)
FIRST_IDENT_JAVASCRIPT = (  # Returns the internal ID of the first document in the grid ('' if none)
    "() => {"  # Start of the function
    "const link = document.querySelector('#GV a[href*=\"fireMe\"]');"  # First document link
    "if (link === null) { return ''; }"  # No rows at all
    "const found = /fireMe\\('(\\d+)'\\)/.exec(link.getAttribute('href') || '');"  # Extract the ID
    "return found ? found[1] : '';"  # Return the ID
    "}"  # End of the function
)  # End of the script
PAGE_CHANGE_CHECK_JAVASCRIPT = (  # True when the label shows the wanted page AND the grid was really replaced
    "(expected) => {"  # Start of the function taking {pageNumber, previousIdent}
    "const pageLabel = document.getElementById('lblPageCount');"  # The page label
    "if (pageLabel === null) { return false; }"  # No label yet
    "const labelText = pageLabel.textContent.replace(/\\u00a0/g, ' ').trim();"  # Clean label text
    "if (!labelText.startsWith('Page ' + expected.pageNumber + ' ')) { return false; }"  # Wrong page
    "if (expected.previousIdent === '') { return true; }"  # Nothing to compare with
    "const link = document.querySelector('#GV a[href*=\"fireMe\"]');"  # First document link now
    "const found = link === null ? null : /fireMe\\('(\\d+)'\\)/.exec(link.getAttribute('href') || '');"  # Its ID
    "const currentIdent = found ? found[1] : '';"  # Empty while the grid is redrawn
    "return currentIdent !== '' && currentIdent !== expected.previousIdent;"  # The grid must differ from before
    "}"  # End of the function
)  # End of the script
CLICK_DOWNLOAD_JAVASCRIPT = (  # Clicks the real PDF link and forces Chrome to download instead of display
    "(url) => {"  # Start of the function
    "const existing = Array.from(document.querySelectorAll('a[href]')).find(link => link.href === url);"  # Real link
    "const link = existing || document.createElement('a');"  # Or a temporary one
    "if (!existing) { link.href = url; document.body.appendChild(link); }"  # Add the temporary link
    "link.setAttribute('download', '');"  # Download instead of display
    "link.click();"  # Click it like a person would
    "if (!existing) { link.remove(); }"  # Remove the temporary link
    "}"  # End of the function
)  # End of the script
GRID_ROW_XPATH = "//table[@id='GV']//tr"  # All rows of the results grid
GRID_LINK_XPATH = (
    "string(.//a[contains(@href,'fireMe')]/@href)"  # The fireMe link of a row
)
ROW_CELLS_XPATH = "./td"  # Standard cells of a row
PDF_LINK_MARKER = "loguser_hash.cfm"  # Identifies a PDF download link
PDF_LINK_XPATH = (
    "//a[contains(@href,'" + PDF_LINK_MARKER + "')]"
)  # All PDF links of a page
PARENT_ROW_XPATH = "ancestor::tr[1]"  # The table row that contains a link
PARENT_ROW_CELLS_XPATH = "./td|./th"  # All cells of a row
FILE_NUMBER_QUERY_NAME = "dmxid"  # URL parameter that holds the file number
GRID_COLUMN_DOCUMENT_ID = 1  # Column of the public document ID in the grid
DETAIL_COLUMN_DESCRIPTION = 1  # Column of the description on the document page
DETAIL_COLUMN_DATE = 3  # Column of the date on the document page
OPEN_DOCUMENT_CALL_PATTERN = re.compile(
    pattern=r"fireMe\('(?P<ident>\d+)'\)"
)  # Extracts the ID from a JS call
PAGE_LABEL_PATTERN = re.compile(
    pattern=r"Page\s+(?P<current>\d+)\s+of\s+(?P<total>\d+)"
)  # Extracts page numbers
LOGOUT_LINK_MARKER = "logout.cfm"  # Only logged-in pages contain this
LOGIN_WORD_MARKER = b"login"  # Indicates a login page
PDF_SIGNATURE = b"%PDF-"  # Start of every PDF
PDF_END_MARKER = b"%%EOF"  # End of every complete PDF
NON_BREAKING_SPACE = "\xa0"  # Non-breaking space character used in HTML
PDF_SIGNATURE_SEARCH_BYTES = 1024  # Bytes read when looking for the PDF signature
PDF_END_SEARCH_BYTES = (
    2048  # Bytes read at the end of a file when looking for the end marker
)
LOGIN_PAGE_SEARCH_BYTES = 5000  # Bytes read when looking for a login page
RETRY_AFTER_HEADER_NAME = "retry-after"  # Header that tells how long to wait
HTTP_RETRYABLE_STATUSES = {
    408,
    425,
    429,
    500,
    502,
    503,
    504,
}  # Usually temporary status codes
SITE_OK = "ok"  # The site answers and we are logged in
SITE_LOGGED_OUT = "logged out"  # The site answers but we are not logged in
SITE_DOWN = "down"  # The site does not answer properly


# ===========================================================================
# SHARED STATE
# ===========================================================================
@dataclass(kw_only=True)  # One object holds every running counter and shared state
class Totals:  # Running counters and shared state
    documents: int = 0  # Documents processed
    saved: int = 0  # PDFs saved
    skipped: int = 0  # PDFs skipped because they already exist
    failed: int = 0  # Problems (documents or files that could not be downloaded)
    suspect: int = 0  # Saved PDFs that still look incomplete
    no_pdfs: int = 0  # Documents without any PDF
    bytes_saved: int = 0  # Total PDF bytes written
    recovered: int = 0  # Documents that failed first but worked later
    restarts: int = 0  # Browser session restarts
    consecutive_failures: int = 0  # Failed documents in a row (circuit breaker)
    breaker_trips: int = 0  # Breaker trips in a row (grows the pause)
    consecutive_no_pdfs: int = 0  # Documents without PDF links in a row (layout check)
    no_pdf_threshold: int = (
        NO_PDF_STREAK_BEFORE_ALERT  # Streak length that triggers the next layout alert
    )
    consecutive_empty_pages: int = (
        0  # Results pages without documents in a row (layout check)
    )
    work_page: Page | None = (
        None  # The dedicated Chrome tab for document pages and downloads
    )
    database: sqlite3.Connection | None = (
        None  # The local database (opened on first use)
    )
    first_run: bool = (
        False  # True when the database was empty at the start (then nothing is announced as new)
    )
    waiting_for_user: bool = False  # True while a prompt waits for you
    started_at: float = field(
        default_factory=time.monotonic
    )  # Moment the script started
    failures: list[FailureRecord] = field(
        default_factory=list
    )  # Every recorded problem
    suspect_files: list[str] = field(
        default_factory=list
    )  # Paths of files saved as suspect
    refused_files: list[str] = field(
        default_factory=list
    )  # Files the server refused: 'ident/file number'
    failed_documents: dict[str, FailedDocument] = field(
        default_factory=dict
    )  # Documents that still fail
    new_documents: dict[str, str] = field(
        default_factory=dict
    )  # IDs not seen in earlier runs: ident -> public ID
    updated_documents: dict[str, list[str]] = field(
        default_factory=dict
    )  # Known IDs that got new files: ident -> file numbers


totals = Totals()  # The one shared state object
logger = logging.getLogger(
    name="assist"
)  # The named logger (handlers are added in setup_logging)
stdin_lines: queue.Queue[str] = queue.Queue()  # Every line typed in the terminal
stdin_state = {"started": False, "closed": False}  # Keyboard thread state
heartbeat_stop = threading.Event()  # Set at the end of the run to stop the heartbeat


# ===========================================================================
# ERRORS RAISED ON PURPOSE
# ===========================================================================
class SessionExpiredError(Exception):  # We are not (or no longer) logged in
    pass  # No custom logic needed


class ForbiddenError(Exception):  # HTTP 403: logged out OR this one file is off limits
    pass  # No custom logic needed


class RetryableFetchError(Exception):  # A temporary problem (worth retrying)
    def __init__(
        self, message: str, wait_seconds: int | None = None
    ) -> None:  # Message plus optional server wait time
        super().__init__(message)  # Store the message
        self.wait_seconds = (
            wait_seconds  # How long the server asked us to wait (or None)
        )


class PermanentFetchError(Exception):  # A problem that retrying will not fix
    pass  # No custom logic needed


class RefusedDownloadError(
    PermanentFetchError
):  # The server refused to send a file (Chrome only says 'canceled')
    pass  # No custom logic needed


class BrowserLostError(Exception):  # The browser tab or window is gone
    pass  # No custom logic needed


# ===========================================================================
# SMALL REUSABLE HELPERS
# ===========================================================================
def format_duration(*, total_seconds: float) -> str:  # Turn seconds into h:mm:ss text
    hours, remainder = divmod(int(total_seconds), 3600)  # Split off whole hours
    minutes, seconds = divmod(remainder, 60)  # Split the rest into minutes and seconds
    return f"{hours}:{minutes:02d}:{seconds:02d}"  # Build the text


def unlink_quietly(*, file_path: Path) -> None:  # Delete a file and never complain
    try:  # Deleting can fail
        file_path.unlink(missing_ok=True)  # Remove the file if it exists
    except OSError:  # Permissions, file in use, ...
        pass  # Nothing more to do


def make_delay_controller(
    *, base_delay: float, maximum_delay: float
) -> tuple[
    Callable[[], float], Callable[[], float], Callable[[], float]
]:  # Three functions that share one private delay
    current_delay = base_delay  # The delay in effect right now

    def slow_down() -> float:  # Double the delay (the server complained)
        nonlocal current_delay  # Change the variable of the enclosing function
        current_delay = min(
            maximum_delay, current_delay * 2
        )  # Double it, but respect the maximum
        return current_delay  # Report the new delay

    def speed_up() -> float:  # Move the delay back towards normal (success)
        nonlocal current_delay  # Change the variable of the enclosing function
        current_delay = max(
            base_delay, current_delay * 0.8
        )  # Shrink it, but never below normal
        return current_delay  # Report the new delay

    def read_delay() -> float:  # Only report the delay
        return current_delay  # Report the current delay

    return slow_down, speed_up, read_delay  # Hand all three functions to the caller


slow_down, speed_up, read_delay = make_delay_controller(
    base_delay=SECONDS_BETWEEN_REQUESTS, maximum_delay=MAXIMUM_DELAY_SECONDS
)  # The one delay controller


def polite_pause() -> None:  # Wait the current adaptive delay, with random jitter
    time.sleep(
        read_delay() * random.uniform(a=JITTER_MINIMUM_FACTOR, b=JITTER_MAXIMUM_FACTOR)
    )  # Sleep delay times a random factor


# ===========================================================================
# LOGGING, ALERTS AND START-UP CHECKS
# ===========================================================================
def setup_logging() -> None:  # Log to the screen and optionally to a rotating file
    if hasattr(
        sys.stdout, "reconfigure"
    ):  # Some stdout objects can change their error handling
        try:  # Reconfiguring can fail
            sys.stdout.reconfigure(
                errors="replace"
            )  # Never crash on characters the console cannot show
        except (OSError, ValueError):  # The console refuses the change
            pass  # Carry on with the default behaviour
    log_format = logging.Formatter(
        fmt=LOG_LINE_FORMAT, datefmt=LOG_DATE_FORMAT
    )  # The log line layout
    logger.setLevel(level=logging.INFO)  # Show INFO messages and above
    logger.propagate = (
        False  # Stop the root logger from printing every line a second time
    )
    logger.handlers.clear()  # Remove old handlers so lines are never printed twice
    screen_handler = logging.StreamHandler(stream=sys.stdout)  # Writes to the terminal
    screen_handler.setFormatter(fmt=log_format)  # Apply the layout
    logger.addHandler(hdlr=screen_handler)  # Attach the handler
    log_file_name = os.environ.get(
        LOG_FILE_ENV_NAME, ""
    ).strip()  # Is the optional log file switched on?
    if not log_file_name:  # No log file wanted
        return  # Nothing more to set up
    try:  # The file may not be writable
        file_handler = logging.handlers.RotatingFileHandler(
            filename=log_file_name,
            maxBytes=LOG_FILE_MAXIMUM_BYTES,
            backupCount=LOG_FILE_BACKUP_COUNT,
            encoding="utf-8",
        )  # Rotates when big
        file_handler.setFormatter(fmt=log_format)  # Apply the same layout
        logger.addHandler(hdlr=file_handler)  # Attach the handler
        logger.info(msg=f"Also logging to {log_file_name}")  # Tell the user
    except OSError as error:  # The file cannot be opened
        logger.warning(
            msg=f"Could not open the log file {log_file_name}: {error}"
        )  # Carry on with the screen only


def send_webhook_alert(
    *, text: str
) -> None:  # Post a message to the optional webhook (never blocks, never fails)
    webhook_url = os.environ.get(
        ALERT_WEBHOOK_ENV_NAME, ""
    ).strip()  # Read the webhook address
    if not webhook_url:  # No webhook configured
        return  # Nothing to do
    if not webhook_url.lower().startswith(
        ("http://", "https://")
    ):  # Only web addresses are allowed (no file: URLs)
        logger.warning(
            msg=f"{ALERT_WEBHOOK_ENV_NAME} must start with http:// or https://"
        )  # Explain the problem
        return  # Do not call it

    def post_message() -> None:  # The work of the background thread
        try:  # The network can fail
            payload = json.dumps(obj={"text": text, "content": text}).encode(
                encoding="utf-8"
            )  # "text" suits Slack, "content" Discord
            request = urllib.request.Request(
                url=webhook_url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )  # Build the request
            with urllib.request.urlopen(
                url=request, timeout=WEBHOOK_TIMEOUT_SECONDS
            ):  # Send it
                pass  # The answer does not matter
        except Exception as error:  # Nothing here may disturb the run
            logger.warning(
                msg=f"Webhook alert failed: {error}"
            )  # Log it (never the URL: it may be a secret)

    threading.Thread(
        target=post_message, name="alert-webhook", daemon=True
    ).start()  # Send in the background


def notify_user(
    *, text: str
) -> None:  # Draw attention to a waiting prompt: terminal bell plus optional webhook
    print("\a", end="", flush=True)  # Ring the terminal bell
    send_webhook_alert(
        text=f"assist downloader: {text}"
    )  # Send the optional webhook message


def wait_for_disk_space() -> None:  # PAUSE (never stop) while the disk is too full
    PDF_FOLDER.mkdir(
        parents=True, exist_ok=True
    )  # The folder must exist so its disk can be measured
    while (
        free_bytes := shutil.disk_usage(path=PDF_FOLDER).free
    ) < MINIMUM_FREE_DISK_BYTES:  # Compare with the safety margin
        logger.warning(
            msg=f"Only {free_bytes // BYTES_PER_MEGABYTE} MB of disk space left. Free some space; checking again in {DISK_WAIT_SECONDS}s"
        )  # Tell the user
        time.sleep(DISK_WAIT_SECONDS)  # Wait, then measure again


def run_preflight_checks() -> (
    None
):  # Check the environment (warnings only, nothing here can stop the script)
    if not sys.stdin.isatty():  # The Enter prompts work best in a real terminal
        logger.warning(
            msg="stdin is not a terminal; if input ends, the Enter prompts continue by themselves"
        )  # Warn only
    try:  # Test that we may write in the output folder
        OUTPUT_FOLDER.mkdir(parents=True, exist_ok=True)  # Make sure the folder exists
        probe_file = OUTPUT_FOLDER / ".write_test"  # A throw-away test file
        probe_file.write_text(data="ok", encoding="utf-8")  # Try to write to it
        probe_file.unlink()  # Delete it again
    except OSError as error:  # Permissions, read-only disk, ...
        logger.warning(
            msg=f"Cannot write to {OUTPUT_FOLDER.resolve()}: {error}"
        )  # Warn and carry on
    try:  # Check the free disk space without being able to crash the start
        wait_for_disk_space()  # Wait here if the disk is too full
    except OSError as error:  # The disk could not be measured
        logger.warning(
            msg=f"Could not measure the free disk space: {error}"
        )  # Warn only


def heartbeat_loop() -> None:  # Prove that the script is still alive
    while not heartbeat_stop.wait(
        timeout=HEARTBEAT_SECONDS
    ):  # Wait, and leave as soon as the stop event is set
        if not totals.waiting_for_user:  # Stay quiet while a prompt waits for you
            logger.info(
                msg=f"Still running. {totals_summary()}"
            )  # Print the running totals


def start_heartbeat_thread() -> None:  # Run the heartbeat in its own thread
    threading.Thread(
        target=heartbeat_loop, name="heartbeat", daemon=True
    ).start()  # Daemon: never blocks the exit


# ===========================================================================
# LOCAL DATABASE (WHAT WAS SEEN AND SAVED IN EARLIER RUNS)
# ===========================================================================
def get_database() -> sqlite3.Connection:  # Open the local database once and reuse it
    if totals.database is None:  # Not opened yet
        OUTPUT_FOLDER.mkdir(parents=True, exist_ok=True)  # Make sure the folder exists
        connection = sqlite3.connect(
            database=str(STATE_DATABASE_FILE)
        )  # Open (or create) the database file
        connection.executescript(
            DATABASE_SCHEMA
        )  # Create the tables if they are missing
        totals.database = connection  # Remember the connection
    return totals.database  # Hand the connection back


def close_database() -> None:  # Close the local database at the end of the run
    if totals.database is not None:  # It was opened
        totals.database.close()  # Close it
        totals.database = None  # Forget it


def log_database_overview() -> None:  # Tell the user what earlier runs already know
    try:  # The database can fail
        connection = get_database()  # Open the database
        document_count = connection.execute(
            "SELECT COUNT(*) FROM documents"
        ).fetchone()[
            0
        ]  # IDs known
        file_count = connection.execute("SELECT COUNT(*) FROM files").fetchone()[
            0
        ]  # Files known
    except sqlite3.Error as error:  # The database cannot be used
        logger.warning(
            msg=f"Could not read {STATE_DATABASE_FILE}: {error}"
        )  # Warn and carry on
        return  # Nothing more to report
    totals.first_run = (
        document_count == 0 and file_count == 0
    )  # An empty database means the very first run
    logger.info(
        msg=f"Earlier runs know {document_count} IDs and {file_count} files. Every ID is checked again; only new files are requested."
    )  # Tell the user


def document_is_known(*, ident: str) -> bool:  # Was this ID seen in an earlier run?
    try:  # The database can fail
        row = (
            get_database()
            .execute(
                "SELECT 1 FROM documents WHERE ident = ? UNION SELECT 1 FROM files WHERE ident = ? LIMIT 1",
                (ident, ident),
            )
            .fetchone()
        )  # Look it up
        return row is not None  # Known if any row exists
    except sqlite3.Error as error:  # The database cannot be used
        logger.warning(
            msg=f"Could not query {STATE_DATABASE_FILE}: {error}"
        )  # Warn and carry on
        return False  # Treat it as unknown


def lookup_saved_path(
    *, ident: str, dmxid: str
) -> Path | None:  # Where did an earlier run save this file (None if never)
    try:  # The database can fail
        row = (
            get_database()
            .execute(
                "SELECT path FROM files WHERE ident = ? AND dmxid = ?", (ident, dmxid)
            )
            .fetchone()
        )  # Look it up
        return Path(row[0]) if row else None  # The recorded path, if any
    except sqlite3.Error as error:  # The database cannot be used
        logger.warning(
            msg=f"Could not query {STATE_DATABASE_FILE}: {error}"
        )  # Warn and carry on
        return None  # Treat it as unknown


def refused_recently(
    *, ident: str, dmxid: str
) -> bool:  # Did the server refuse this file within the last REFUSED_RECHECK_HOURS?
    try:  # The database can fail
        row = (
            get_database()
            .execute(
                "SELECT refused_at FROM refused WHERE ident = ? AND dmxid = ?",
                (ident, dmxid),
            )
            .fetchone()
        )  # Look it up
    except sqlite3.Error as error:  # The database cannot be used
        logger.warning(
            msg=f"Could not query {STATE_DATABASE_FILE}: {error}"
        )  # Warn and carry on
        return False  # Treat it as unknown
    if row is None:  # Never refused
        return False  # Ask for it
    return datetime.now() - datetime.fromisoformat(row[0]) < timedelta(
        hours=REFUSED_RECHECK_HOURS
    )  # Recent enough to leave it alone


def record_refused_file(
    *, document_record: DocumentRecord, pdf_link: PdfLink, reason: str
) -> None:  # Remember that the server refused a file
    try:  # The database can fail
        with get_database() as connection:  # One transaction (committed on success)
            connection.execute(  # Insert or replace the row of this file
                "INSERT OR REPLACE INTO refused (ident, dmxid, url, refused_at, reason) VALUES (?, ?, ?, ?, ?)",  # The statement
                (
                    document_record["ident"],
                    pdf_link["dmxid"],
                    pdf_link["url"],
                    datetime.now().isoformat(timespec="seconds"),
                    reason,
                ),  # The values
            )  # End of the statement
    except sqlite3.Error as error:  # The database cannot be used
        logger.warning(
            msg=f"Could not write to {STATE_DATABASE_FILE}: {error}"
        )  # Warn and carry on


def record_file(
    *,
    document_record: DocumentRecord,
    pdf_link: PdfLink,
    saved_path: Path,
    download: PdfDownload,
    suspect: bool,
) -> None:  # Remember one saved PDF
    try:  # The database can fail
        with get_database() as connection:  # One transaction (committed on success)
            connection.execute(  # Insert or replace the row of this file
                "INSERT OR REPLACE INTO files (ident, dmxid, url, path, bytes, sha256, suspect, saved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",  # The statement
                (
                    document_record["ident"],
                    pdf_link["dmxid"],
                    pdf_link["url"],
                    str(saved_path),
                    download.size,
                    download.sha256,
                    int(suspect),
                    datetime.now().isoformat(timespec="seconds"),
                ),  # The values
            )  # End of the statement
            connection.execute(
                "DELETE FROM refused WHERE ident = ? AND dmxid = ?",
                (document_record["ident"], pdf_link["dmxid"]),
            )  # A saved file is no longer refused
    except sqlite3.Error as error:  # The database cannot be used
        logger.warning(
            msg=f"Could not write to {STATE_DATABASE_FILE}: {error}"
        )  # Warn and carry on


def remember_file_from_disk(
    *, document_record: DocumentRecord, pdf_link: PdfLink, saved_path: Path
) -> (
    None
):  # Record a file that is on disk but unknown to the database (no request involved)
    try:  # Reading the file can fail
        download = describe_downloaded_file(
            partial_path=saved_path
        )  # Inspect and hash the file on the local disk
    except (
        OSError,
        PermanentFetchError,
        RetryableFetchError,
        SessionExpiredError,
    ):  # Not readable or not a PDF
        return  # Leave it alone
    record_file(
        document_record=document_record,
        pdf_link=pdf_link,
        saved_path=saved_path,
        download=download,
        suspect=SUSPECT_MARKER in saved_path.name,
    )  # Add it to the database


def record_document_check(
    *, document_record: DocumentRecord, pdf_count: int
) -> None:  # Remember that an ID was checked now
    now = datetime.now().isoformat(timespec="seconds")  # The time of this check
    try:  # The database can fail
        with get_database() as connection:  # One transaction (committed on success)
            connection.execute(  # Insert the ID, or update its last check
                "INSERT INTO documents (ident, doc_id, first_seen, last_checked, pdf_count) VALUES (?, ?, ?, ?, ?) "  # New ID
                "ON CONFLICT(ident) DO UPDATE SET doc_id = excluded.doc_id, last_checked = excluded.last_checked, pdf_count = excluded.pdf_count",  # Known ID
                (
                    document_record["ident"],
                    document_record["doc_id"],
                    now,
                    now,
                    pdf_count,
                ),  # The values
            )  # End of the statement
    except sqlite3.Error as error:  # The database cannot be used
        logger.warning(
            msg=f"Could not write to {STATE_DATABASE_FILE}: {error}"
        )  # Warn and carry on


def note_new_document(
    *, document_record: DocumentRecord
) -> None:  # Announce an ID that no earlier run has seen
    ident = document_record["ident"]  # Internal ID
    if (
        totals.first_run or ident in totals.new_documents
    ):  # The first run knows nothing, and each ID is announced once
        return  # Nothing to announce
    totals.new_documents[ident] = document_record[
        "doc_id"
    ]  # Remember it for the final summary
    logger.info(msg=f"NEW ID {ident} ({document_record['doc_id']})")  # Announce it


def note_new_file(
    *, document_record: DocumentRecord, pdf_link: PdfLink, document_is_new: bool
) -> None:  # Announce a new file on an ID that earlier runs already knew
    if document_is_new:  # New IDs were announced already
        return  # Nothing more to say
    ident = document_record["ident"]  # Internal ID
    totals.updated_documents.setdefault(ident, []).append(
        pdf_link["dmxid"]
    )  # Remember it for the final summary
    logger.info(
        msg=f"NEW FILE on known ID {ident} ({document_record['doc_id']}): file {pdf_link['dmxid']}"
    )  # Announce it


# ===========================================================================
# PARSING THE WEB PAGES
# ===========================================================================
def clean_text(
    *, html_element: Any
) -> str:  # Extract and clean the text of an HTML element
    return " ".join(
        html_element.text_content().replace(NON_BREAKING_SPACE, " ").split()
    )  # Replace nbsp, collapse whitespace


def get_cell_text(
    *, cell_texts: list[str], column_index: int
) -> str:  # Safely get the text of one cell
    return (
        cell_texts[column_index] if column_index < len(cell_texts) else ""
    )  # Text if the index is valid, else empty


def decode_body(
    *, response_body: bytes
) -> str:  # Turn bytes into text without ever failing
    return response_body.decode(
        encoding="utf-8", errors="replace"
    )  # Replace undecodable bytes


def parse_results_grid(
    *, html_text: str
) -> list[DocumentRecord]:  # Parse the search results HTML
    html_document = lxml_html.document_fromstring(
        html=html_text
    )  # Parse the HTML string
    document_records: list[DocumentRecord] = []  # The records found
    seen_idents: set[str] = (
        set()
    )  # IDs already seen on this page (duplicate rows are skipped)
    for table_row in html_document.xpath(
        GRID_ROW_XPATH
    ):  # Every row of the results table
        match_found = OPEN_DOCUMENT_CALL_PATTERN.search(
            string=table_row.xpath(GRID_LINK_XPATH) or ""
        )  # The document ID link
        if not match_found:  # Header row, footer row, ...
            continue  # Skip it
        ident = match_found.group("ident")  # The internal document ID
        if ident in seen_idents:  # A duplicate row
            continue  # Skip it
        seen_idents.add(ident)  # Remember this ID
        cell_texts = [
            clean_text(html_element=cell) for cell in table_row.xpath(ROW_CELLS_XPATH)
        ]  # Clean text of all cells
        document_records.append(
            {
                "ident": ident,
                "doc_id": get_cell_text(
                    cell_texts=cell_texts, column_index=GRID_COLUMN_DOCUMENT_ID
                ),
            }
        )  # Add the record
    return document_records  # Hand the records back


def parse_detail_page(
    *, html_text: str, page_url: str
) -> list[PdfLink]:  # Parse a document page for its PDF links
    html_document = lxml_html.document_fromstring(
        html=html_text
    )  # Parse the HTML string
    pdf_links: list[PdfLink] = []  # The links found
    seen_file_numbers: set[str] = (
        set()
    )  # File numbers already processed (duplicates are skipped)
    for link_element in html_document.xpath(
        PDF_LINK_XPATH
    ):  # Every PDF link of the page
        absolute_url = urljoin(
            base=page_url, url=link_element.get(key="href", default="")
        )  # lxml already decoded entities, so never decode again
        file_number = parse_qs(qs=urlparse(url=absolute_url).query).get(
            FILE_NUMBER_QUERY_NAME, [""]
        )[
            0
        ]  # The file number from the URL
        if not file_number:  # The link has no file number at all
            file_number = f"{FALLBACK_FILE_NUMBER}-{len(pdf_links) + 1}"  # Invent a unique one so file names never collide
        if file_number in seen_file_numbers:  # Already seen
            continue  # Skip it
        seen_file_numbers.add(file_number)  # Remember it
        parent_rows = link_element.xpath(
            PARENT_ROW_XPATH
        )  # The table row that holds the link
        cell_texts = (
            [
                clean_text(html_element=cell)
                for cell in parent_rows[0].xpath(PARENT_ROW_CELLS_XPATH)
            ]
            if parent_rows
            else []
        )  # Clean text of its cells
        pdf_links.append(
            {  # Add the link
                "url": absolute_url,  # Absolute download address
                "dmxid": file_number,  # File number
                "description": get_cell_text(
                    cell_texts=cell_texts, column_index=DETAIL_COLUMN_DESCRIPTION
                ),  # Description text
                "date": get_cell_text(
                    cell_texts=cell_texts, column_index=DETAIL_COLUMN_DATE
                ),  # Date text
            }
        )  # End of the link
    return pdf_links  # Hand the links back


# ===========================================================================
# FOLDER AND FILE NAMES
# ===========================================================================
def make_safe_name_part(
    *, text: str | None, maximum_length: int, fallback: str
) -> str:  # Sanitise a string for use in a path
    # Keep original casing, just ensure it's a string
    raw_text = str(text or "")

    # Replace absolutely everything that is not A-Z, a-z, 0-9, or _ with a single _
    cleaned_text = re.sub(pattern=r"[^A-Za-z0-9_]", repl="_", string=raw_text)

    # Collapse multiple underscores into a single underscore
    cleaned_text = re.sub(pattern=r"_+", repl="_", string=cleaned_text)

    # Strip underscores from the edges so it never starts or ends with _
    cleaned_text = cleaned_text.strip("_")

    shortened_text = cleaned_text[:maximum_length]  # Cut to the maximum length
    cut_inside_a_word = (
        len(cleaned_text) > maximum_length and cleaned_text[maximum_length] != "_"
    )  # Did the cut split a word?

    if (
        cut_inside_a_word and "_" in shortened_text
    ):  # A word was cut and a separator exists
        shortened_text = shortened_text.rsplit("_", 1)[
            0
        ]  # Trim back to the last separator

    safe_text = shortened_text.strip("_") or fallback.replace("-", "_").strip(
        "_"
    )  # The clean text, or the fallback if empty

    if (
        safe_text.upper() in RESERVED_WINDOWS_NAMES
    ):  # Windows cannot create CON, NUL, COM1, ... (case-insensitive check)
        safe_text = (
            safe_text + "_SAFE"
        )  # Append _SAFE so we don't start with an underscore

    return safe_text


def format_date(
    *, date_text: str
) -> str:  # Reformat a date string for use in file names
    for date_format in DATE_INPUT_FORMATS:  # Try every known layout in turn
        try:  # The layout may not fit
            return datetime.strptime(date_text.strip(), date_format).strftime(
                DATE_OUTPUT_FORMAT
            )  # Parse and reformat
        except ValueError:  # This layout does not fit
            continue  # Try the next one
    return make_safe_name_part(
        text=date_text,
        maximum_length=FILE_NUMBER_MAXIMUM_LENGTH,
        fallback=FALLBACK_DATE_TEXT,
    )  # Sanitised raw text


def build_folder_name(
    *, document_record: DocumentRecord
) -> str:  # Folder name of a document
    fallback_name = (
        FALLBACK_FOLDER_PREFIX + document_record["ident"]
    )  # Fallback built from the internal ID

    folder_name = make_safe_name_part(
        text=document_record["doc_id"],
        maximum_length=FOLDER_NAME_MAXIMUM_LENGTH,
        fallback=fallback_name,
    )  # Sanitised document ID

    return folder_name


def build_file_name(
    *, document_folder: Path, pdf_link: PdfLink
) -> str:  # File name of a PDF (the folder already carries the document ID)
    # Format the date and strictly clean it to A-Z, a-z, 0-9, and _
    date_part = format_date(date_text=pdf_link["date"])
    date_part = re.sub(pattern=r"[^A-Za-z0-9_]", repl="_", string=date_part)
    date_part = re.sub(pattern=r"_+", repl="_", string=date_part).strip("_")

    number_part = make_safe_name_part(
        text=pdf_link["dmxid"],
        maximum_length=FILE_NUMBER_MAXIMUM_LENGTH,
        fallback=FALLBACK_FILE_NUMBER,
    )  # Sanitised file number

    fixed_length = (
        len(date_part)
        + len(number_part)
        + 2  # for the two underscores used to join the three parts
        + len(SUSPECT_MARKER)
        + len(PDF_FILE_EXTENSION)
        + len(PARTIAL_FILE_EXTENSION)
    )  # Everything except the description

    description_limit = DESCRIPTION_MAXIMUM_LENGTH  # Start with the normal limit
    if os.name == "nt":  # Windows has a 260 character path limit
        room_left = (
            MAXIMUM_WINDOWS_PATH_LENGTH
            - len(str(document_folder.resolve()))
            - 1
            - fixed_length
        )  # Characters left for the description
        description_limit = max(
            MINIMUM_DESCRIPTION_LENGTH, min(description_limit, room_left)
        )  # Shorten the description if needed

    description_part = make_safe_name_part(
        text=pdf_link["description"],
        maximum_length=description_limit,
        fallback=FALLBACK_DESCRIPTION,
    )  # Sanitised description

    # Join the parts using underscores (filtering out any empty parts)
    file_name_base = "_".join(filter(None, [description_part, date_part, number_part]))

    # Final safety pass: no double underscores, and NO underscores right before the .pdf
    file_name_base = re.sub(r"_+", "_", file_name_base).strip("_")

    file_name = file_name_base + PDF_FILE_EXTENSION  # Join the parts
    assert (
        len(file_name) < 255
    ), "file name too long"  # Sanity check (an AssertionError is caught per document)
    return file_name  # Hand the name back


def suspect_path_for(
    *, target_path: Path
) -> Path:  # The name used for PDFs that look incomplete
    return target_path.with_name(
        target_path.stem + SUSPECT_MARKER + PDF_FILE_EXTENSION
    )  # For example "x_2022-03-14_A1.suspect.pdf"


# ===========================================================================
# STATUS CODES AND CONTENT CHECKS
# ===========================================================================
def parse_retry_after_seconds(
    *, header_value: str | None
) -> int | None:  # Convert a Retry-After header into seconds
    if not header_value:  # The server sent no such header
        return None  # There is no wait time
    stripped_value = header_value.strip()  # Remove stray spaces
    if stripped_value.isdigit():  # A plain number of seconds
        seconds = int(stripped_value)  # Convert it
    else:  # Possibly a date such as "Wed, 21 Oct 2026 07:28:00 GMT"
        try:  # The date may be unreadable
            retry_moment = parsedate_to_datetime(
                data=stripped_value
            )  # Parse the HTTP date
            seconds = int(
                (retry_moment - datetime.now(tz=retry_moment.tzinfo)).total_seconds()
            )  # Seconds from now until then
        except (TypeError, ValueError, OverflowError):  # Something we do not handle
            return None  # Fall back to the normal backoff
    return max(
        0, min(seconds, MAXIMUM_RETRY_AFTER_SECONDS)
    )  # Keep the value between 0 and the maximum


def check_http_status(
    *, status_code: int, status_text: str, headers: dict[str, str], label: str
) -> None:  # Turn bad HTTP status codes into the right error
    match status_code:  # Look at the status code
        case 401:  # Not logged in (any more)
            raise SessionExpiredError(
                f"{label}: HTTP 401 - not logged in"
            )  # Ask the caller to log in again
        case (
            403
        ):  # Logged out OR this one file is off limits; the caller finds out which
            raise ForbiddenError(
                f"{label}: HTTP 403 - access denied"
            )  # Let the caller decide
        case code if (
            code in HTTP_RETRYABLE_STATUSES
        ):  # 408, 429, 5xx, ...: probably temporary
            wait_seconds = parse_retry_after_seconds(
                header_value=headers.get(RETRY_AFTER_HEADER_NAME)
            )  # Respect Retry-After if given
            raise RetryableFetchError(
                f"{label}: HTTP {code} {status_text} (temporary)", wait_seconds
            )  # Ask the caller to retry later
        case 204:  # Success, but no content
            raise PermanentFetchError(
                f"{label}: HTTP 204 - the server sent no content"
            )  # Nothing to use
        case code if (
            not 200 <= code < 300
        ):  # Any other non-2xx status (404, 410, 400, ...)
            raise PermanentFetchError(
                f"{label}: HTTP {code} {status_text}"
            )  # Retrying will not help
        case _:  # The normal 2xx case
            pass  # Nothing to complain about


def looks_like_login_page(
    *, response_body: bytes
) -> bool:  # Guess whether the server sent a login page
    start_of_body = response_body[
        :LOGIN_PAGE_SEARCH_BYTES
    ].lower()  # Only the beginning, in lower case
    has_login_word = LOGIN_WORD_MARKER in start_of_body  # Does the word "login" appear?
    has_logout_link = (
        LOGOUT_LINK_MARKER.encode() in response_body.lower()
    )  # Only logged-in pages have a logout link
    return (
        has_login_word and not has_logout_link
    )  # A login page mentions "login" but has no logout link


def validate_detail_page(
    *, response: Response, response_body: bytes
) -> None:  # Check a document page is real and logged in
    if not response_body.strip():  # The page is empty
        raise RetryableFetchError(
            "the document page was empty"
        )  # Empty pages are usually a glitch
    if (
        LOGOUT_LINK_MARKER.encode() not in response_body.lower()
    ):  # No logout link means we were logged out
        raise SessionExpiredError(
            "the document page has no Logout link"
        )  # Ask the caller to log in again


def raise_for_non_pdf(
    *, response_body: bytes, description: str
) -> None:  # Decide WHY a file is not a PDF (always raises)
    if looks_like_login_page(response_body=response_body):  # It is really a login page
        raise SessionExpiredError(
            "got a login page instead of a PDF"
        )  # Ask the caller to log in again
    if not response_body.strip():  # The body is empty
        raise RetryableFetchError("the PDF response was empty")  # Treat as a glitch
    raise PermanentFetchError(
        f"the downloaded file is not a PDF ({description}, {len(response_body)} bytes)"
    )  # Retrying will not help


# ===========================================================================
# CHROME TABS, LOGIN AND THE RETRY ENGINE
# ===========================================================================
def get_working_page(
    *, browser_context: BrowserContext
) -> Page:  # The first open tab (the results grid tab)
    for open_page in browser_context.pages:  # Look at every tab
        if not open_page.is_closed():  # Skip closed tabs
            return open_page  # Use the first tab that is still open
    return browser_context.new_page()  # No open tab left, so open a new one


def get_work_page(
    *, browser_context: BrowserContext
) -> Page:  # The dedicated tab for document pages and downloads
    work_page = totals.work_page  # The tab remembered from earlier
    if (
        work_page is None
        or work_page.is_closed()
        or work_page.context is not browser_context
    ):  # Missing, closed or from a crashed session
        work_page = (
            browser_context.new_page()
        )  # Open a fresh tab (the results grid tab is never touched)
        totals.work_page = work_page  # Remember it
    return work_page  # Hand the tab back


def navigate_with_log(
    *, browser_context: BrowserContext, url: str, label: str
) -> tuple[Response, bytes, str]:  # VISIT a page in Chrome
    logger.info(msg=f"{label}: Opening {url}")  # Log the address
    started_at = time.monotonic()  # Remember when the visit started
    work_page = get_work_page(browser_context=browser_context)  # The dedicated tab
    response = work_page.goto(
        url=url, wait_until="domcontentloaded", timeout=REQUEST_TIMEOUT_MS
    )  # Let Chrome load the page like a person
    if response is None:  # The navigation was replaced by another one
        raise RetryableFetchError(f"{label}: Chrome gave no response")  # Try again
    page_body = work_page.content().encode(
        encoding="utf-8"
    )  # The HTML as Chrome rendered it
    logger.info(
        msg=f"{label}: HTTP {response.status} {response.status_text}, {len(page_body)} bytes in {time.monotonic() - started_at:.1f}s"
    )  # Log status, size, duration
    return (
        response,
        page_body,
        work_page.url,
    )  # The response, the page and where Chrome ended up


def check_site_state(
    *, browser_context: BrowserContext
) -> str:  # Is the site up, and are we logged in?
    try:  # The visit can fail
        response, response_body, _ = navigate_with_log(
            browser_context=browser_context,
            url=DETAIL_PAGE_URL_TEMPLATE.format(LOGIN_CHECK_IDENT),
            label="Login check",
        )  # Visit a test page
    except PlaywrightError as error:  # Network error or timeout
        logger.warning(msg=f"Login check failed: {error}")  # Log why
        return SITE_DOWN  # The site does not answer
    if response.status in HTTP_RETRYABLE_STATUSES:  # Overloaded or broken
        return SITE_DOWN  # The site does not answer properly
    if (
        response.ok and LOGOUT_LINK_MARKER.encode() in response_body.lower()
    ):  # 2xx and a logout link
        return SITE_OK  # Up and logged in
    return SITE_LOGGED_OUT  # Up, but not logged in


def page_shows_logout_link(
    *, page: Page
) -> bool:  # Does a tab that is already open show the logout link? (no request)
    try:  # The tab may be closed or busy
        return (
            LOGOUT_LINK_MARKER in page.content().lower()
        )  # Only logged-in pages contain the logout link
    except PlaywrightError:  # The tab cannot be inspected
        return False  # Treat it as "not shown"


def is_logged_in(
    *, browser_context: BrowserContext
) -> bool:  # Is the session authenticated? (costs one request)
    return (
        check_site_state(browser_context=browser_context) == SITE_OK
    )  # Only "up and logged in" counts


def ensure_login(
    *, browser_context: BrowserContext
) -> bool:  # Prompt the user to log in if needed; True if a log-in was needed
    if is_logged_in(
        browser_context=browser_context
    ):  # Already logged in (the one request that cannot be avoided)
        return False  # Nothing was needed
    login_page = (
        browser_context.new_page()
    )  # A NEW tab, so the results grid tab is never navigated away
    try:  # The extra tab must always be closed
        logger.info(msg="Please log in again in the browser window")  # Notify the user
        try:  # The start page may not load
            login_page.goto(
                url=START_PAGE_URL, timeout=REQUEST_TIMEOUT_MS
            )  # Open the login page
        except PlaywrightError as error:  # The page did not load
            logger.warning(
                msg=f"Could not open the log-in page: {error}"
            )  # Tell the user
        wait_for_enter(
            browser_page=login_page, message=LOGIN_TAB_MESSAGE
        )  # Wait until the user has logged in
        while not (
            page_shows_logout_link(page=login_page)
            or is_logged_in(browser_context=browser_context)
        ):  # Look at the open tab first, ask the server only if needed
            wait_for_enter(
                browser_page=login_page, message=NOT_LOGGED_IN_REMINDER
            )  # Remind the user
    finally:  # Always runs
        try:  # Closing can fail
            login_page.close()  # Close the extra tab
        except PlaywrightError:  # Already closed
            pass  # Nothing more to do
    logger.info(msg="Login OK")  # Log the success
    return True  # A log-in was needed and is done


def handle_login_problem(
    *,
    browser_context: BrowserContext,
    label: str,
    error: Exception,
    relogins: int,
    backoff_seconds: float,
) -> bool:  # React to a 401/403; True if a log-in happened
    if isinstance(error, ForbiddenError) and is_logged_in(
        browser_context=browser_context
    ):  # A 403 while the session is fine
        raise PermanentFetchError(
            f"{label}: access denied (HTTP 403) although the session is valid"
        ) from error  # This file is off limits
    if relogins > MAXIMUM_RELOGINS_PER_REQUEST:  # Logging in again does not help
        raise PermanentFetchError(
            f"{label}: still logged out after {relogins - 1} log-ins"
        ) from error  # Give up on this request only
    try:  # The log-in itself can hit a browser problem
        login_was_needed = ensure_login(
            browser_context=browser_context
        )  # Let the user log in if needed
    except PlaywrightError as login_error:  # The browser failed during the log-in
        logger.warning(msg=f"{label}: log-in failed: {login_error}")  # Tell the user
        time.sleep(backoff_seconds)  # Wait a little
        return True  # Do not count this attempt (relogins is limited anyway)
    if not login_was_needed:  # The session looked fine
        time.sleep(backoff_seconds)  # Pause instead of hammering the server
    return login_was_needed  # Tell the caller whether a log-in happened


def run_with_retries(
    *, browser_context: BrowserContext, label: str, attempt_function: AttemptFunction
) -> Any:  # The ONE retry loop every download uses
    last_problem = "unknown problem"  # The most recent problem, for the final message
    relogins = 0  # How often this request needed a new log-in
    attempt_number = 0  # Normal attempts made
    while attempt_number < RETRY_ATTEMPTS:  # Try up to RETRY_ATTEMPTS times
        attempt_number += 1  # Count this attempt
        backoff_seconds = (
            SECONDS_PER_RETRY_STEP * attempt_number
        )  # Progressive backoff: 2s, 4s, 6s, ...
        try:  # Catch every kind of problem
            result = attempt_function(
                attempt_label=f"{label} (try {attempt_number}/{RETRY_ATTEMPTS})"
            )  # Do the actual work
            speed_up()  # The server is happy, so move the delay back towards normal
            return result  # Everything is fine
        except (
            SessionExpiredError,
            ForbiddenError,
        ) as error:  # We may have been logged out
            last_problem = str(error)  # Remember the problem
            logger.warning(msg=f"{label}: {error}")  # Tell the user
            relogins += 1  # Count the log-in request
            if handle_login_problem(
                browser_context=browser_context,
                label=label,
                error=error,
                relogins=relogins,
                backoff_seconds=backoff_seconds,
            ):  # A log-in happened
                attempt_number -= 1  # A log-in does not use up a normal attempt
            continue  # Try again (the pause was already made)
        except RetryableFetchError as error:  # A temporary server problem
            last_problem = str(error)  # Remember the problem
            backoff_seconds = (
                error.wait_seconds
                if error.wait_seconds is not None
                else backoff_seconds
            )  # Prefer the server's wait time
            slow_down()  # Be gentler to the server from now on
            logger.warning(
                msg=f"{label}: {error}; waiting {backoff_seconds}s"
            )  # Tell the user
        except PermanentFetchError:  # Retrying cannot fix this
            raise  # Pass it straight up
        except PlaywrightError as error:  # Network error, timeout or closed browser
            last_problem = f"network error: {error}"  # Remember the problem
            logger.warning(
                msg=f"{label}: {last_problem}; waiting {backoff_seconds}s"
            )  # Tell the user
        if attempt_number < RETRY_ATTEMPTS:  # Only wait if another attempt follows
            time.sleep(backoff_seconds)  # Wait before trying again
    raise PermanentFetchError(
        f"{label}: gave up after {RETRY_ATTEMPTS} tries ({last_problem})"
    )  # All attempts failed


def fetch_page_with_retries(
    *,
    browser_context: BrowserContext,
    url: str,
    label: str,
    validate_body: BodyValidator,
) -> tuple[str, bytes]:  # Visit a page in Chrome safely
    def attempt(*, attempt_label: str) -> tuple[str, bytes]:  # One attempt
        response, page_body, final_url = navigate_with_log(
            browser_context=browser_context, url=url, label=attempt_label
        )  # Let Chrome visit the page
        check_http_status(
            status_code=response.status,
            status_text=response.status_text,
            headers=response.headers,
            label=label,
        )  # Check the status code
        validate_body(
            response=response, response_body=page_body
        )  # Check the content is what we expected
        return final_url, page_body  # Hand the result back

    return run_with_retries(
        browser_context=browser_context, label=label, attempt_function=attempt
    )  # Run it with all the retry rules


def wait_for_site_recovery(
    *, browser_context: BrowserContext, wait_seconds: float
) -> None:  # Wait until the site is up and we are logged in
    while True:  # Until the site is healthy again
        time.sleep(wait_seconds)  # Pause
        state = check_site_state(
            browser_context=browser_context
        )  # Is the site up? Are we logged in?
        if state == SITE_OK:  # All good
            return  # Resume
        if state == SITE_LOGGED_OUT:  # The session is gone
            try:  # The log-in can hit a browser problem
                ensure_login(browser_context=browser_context)  # Ask the user to log in
            except PlaywrightError as error:  # The browser failed
                logger.warning(
                    msg=f"Log-in after the pause failed: {error}"
                )  # Tell the user
            return  # Resume
        wait_seconds = min(
            MAXIMUM_PAGING_BACKOFF_SECONDS, wait_seconds * 2
        )  # The site is still down: wait longer
        logger.error(
            msg=f"The site still does not answer properly; waiting {wait_seconds}s"
        )  # Tell the user


def circuit_breaker(
    *, browser_context: BrowserContext, document_ok: bool
) -> None:  # Pause the run when many documents fail in a row
    if document_ok:  # A success resets everything
        totals.consecutive_failures = 0  # No streak
        totals.breaker_trips = 0  # The next trip starts with a short pause again
        return  # Nothing else to do
    totals.consecutive_failures += 1  # One more failure in a row
    if totals.consecutive_failures < CIRCUIT_BREAKER_FAILURES:  # Not enough yet
        return  # Keep going
    totals.breaker_trips += 1  # Count this trip
    wait_seconds = min(
        MAXIMUM_PAGING_BACKOFF_SECONDS,
        CIRCUIT_BREAKER_BASE_SECONDS * 2 ** (totals.breaker_trips - 1),
    )  # Longer pause with every trip
    logger.error(
        msg=f"{totals.consecutive_failures} documents in a row failed (site down, throttling or session lost?). Pausing {wait_seconds}s"
    )  # Explain
    wait_for_site_recovery(
        browser_context=browser_context, wait_seconds=wait_seconds
    )  # Wait until the site is healthy
    totals.consecutive_failures = 0  # Start counting again
    logger.info(msg="Resuming after the pause")  # Tell the user


# ===========================================================================
# CHROME DOWNLOADS
# ===========================================================================
def describe_downloaded_file(
    *, partial_path: Path
) -> (
    PdfDownload
):  # Check a file saved by Chrome and describe it (raises if it is not a PDF)
    size = partial_path.stat().st_size  # Number of bytes on disk
    with partial_path.open(mode="rb") as saved_file:  # Open the file for reading
        head = saved_file.read(PDF_SIGNATURE_SEARCH_BYTES)  # Read the beginning
        if PDF_SIGNATURE not in head:  # Not a PDF
            body = head + saved_file.read(
                MAXIMUM_NON_PDF_BYTES
            )  # Read enough to recognise a login page
            raise_for_non_pdf(
                response_body=body, description="saved by Chrome"
            )  # Always raises
        if size < MINIMUM_PDF_BYTES:  # Absurdly small
            raise RetryableFetchError(
                f"the PDF is only {size} bytes (probably cut off)"
            )  # Treat as a glitch
        saved_file.seek(
            max(0, size - PDF_END_SEARCH_BYTES)
        )  # Jump near the end of the file
        tail = saved_file.read()  # Read the last bytes
        saved_file.seek(0)  # Back to the start for the hash
        digest = hashlib.sha256()  # Running hash of the file
        while chunk := saved_file.read(HASH_CHUNK_BYTES):  # Read in pieces
            digest.update(chunk)  # Hash the piece
    return PdfDownload(
        size=size,
        sha256=digest.hexdigest(),
        has_end_marker=PDF_END_MARKER in tail,
        partial_path=partial_path,
    )  # Describe the file


def raise_for_download_failure(
    *, failure: str, label: str
) -> None:  # Turn Chrome's failure text into the right error
    if (
        failure.lower() == "canceled"
    ):  # Chrome reports exactly this when the server answered with an error status (403, 404, 500, ...)
        raise RefusedDownloadError(
            f"{label}: the server refused the download (Chrome only reports '{failure}'; probably HTTP 403, 404 or 500)"
        )  # One attempt is enough
    raise RetryableFetchError(
        f"{label}: Chrome download failed ({failure})"
    )  # Anything else may be temporary


def let_chrome_download(
    *, work_page: Page, url: str, label: str
) -> Download:  # Make Chrome download one file and wait for it to finish
    with work_page.expect_download(
        timeout=REQUEST_TIMEOUT_MS
    ) as download_info:  # Wait for Chrome to start a download
        work_page.evaluate(
            expression=CLICK_DOWNLOAD_JAVASCRIPT, arg=url
        )  # Click the real link (as a download link)
    download = download_info.value  # The download Chrome started
    failure = download.failure()  # Waits for the end; None means success
    if failure:  # Chrome could not finish the download
        raise_for_download_failure(
            failure=failure, label=label
        )  # Raise the matching error
    return download  # Hand the finished download back


def fetch_pdf_with_retries(
    *,
    browser_context: BrowserContext,
    url: str,
    label: str,
    detail_url: str,
    partial_path: Path,
) -> PdfDownload:  # Let CHROME download one PDF to a temporary file
    def attempt(*, attempt_label: str) -> PdfDownload:  # One attempt
        work_page = get_work_page(browser_context=browser_context)  # The dedicated tab
        if (
            work_page.url != detail_url
        ):  # The link must be clicked on the document page itself
            work_page.goto(
                url=detail_url,
                wait_until="domcontentloaded",
                timeout=REQUEST_TIMEOUT_MS,
            )  # Go back to the document page
        wait_for_disk_space()  # Make sure there is room (waits instead of stopping)
        partial_path.parent.mkdir(
            parents=True, exist_ok=True
        )  # Make sure the folder exists
        logger.info(
            msg=f"{attempt_label}: Chrome is downloading {url}"
        )  # Log the address
        started_at = time.monotonic()  # Remember when the download started
        download = let_chrome_download(
            work_page=work_page, url=url, label=label
        )  # Let Chrome download the file
        download.save_as(
            path=str(partial_path)
        )  # Copy Chrome's temporary download to our temporary file
        try:  # A rejected file must never stay behind
            result = describe_downloaded_file(
                partial_path=partial_path
            )  # Check it really is a PDF
        except BaseException:  # Anything went wrong (including Ctrl+C)
            unlink_quietly(file_path=partial_path)  # Remove the rejected file
            raise  # Pass the problem on
        logger.info(
            msg=f"{attempt_label}: Chrome saved {result.size} bytes in {time.monotonic() - started_at:.1f}s"
        )  # Log size and duration
        return result  # Hand the result back

    return run_with_retries(
        browser_context=browser_context, label=label, attempt_function=attempt
    )  # Run it with all the retry rules


# ===========================================================================
# BROWSER AND KEYBOARD
# ===========================================================================
def handle_alert_dialog(
    dialog: Dialog,
) -> (
    None
):  # Automatically accept browser alerts (Playwright passes the dialog positionally)
    try:  # The dialog may vanish before we answer it
        logger.info(
            msg=f"Browser alert accepted: {dialog.message[:100]!r}"
        )  # Log the alert text so it is never a mystery
        dialog.accept()  # Click OK
    except PlaywrightError as error:  # The dialog was already gone
        logger.warning(
            msg=f"Could not answer a browser alert: {error}"
        )  # Log and carry on


def open_browser_context(
    *, playwright_instance: Playwright
) -> BrowserContext:  # Launch and configure the browser
    BROWSER_PROFILE_FOLDER.mkdir(
        parents=True, exist_ok=True
    )  # Create the browser profile folder
    try:  # The browser may not be installed
        browser_context = playwright_instance.chromium.launch_persistent_context(
            user_data_dir=str(BROWSER_PROFILE_FOLDER),
            headless=not SHOW_BROWSER_WINDOW,
            accept_downloads=True,
        )  # Launch the persistent browser
    except PlaywrightError as error:  # Chromium could not start
        raise RuntimeError(
            f"Could not start Chromium ({error}). Try: playwright install --with-deps chromium"
        ) from error  # The main loop retries later
    browser_context.on(
        event=DIALOG_EVENT_NAME, f=handle_alert_dialog
    )  # Accept dialogs automatically
    return browser_context  # Hand the browser back


def stdin_reader_loop() -> (
    None
):  # The loop of the keyboard thread (works on Linux, macOS and Windows)
    while True:  # Keep reading lines
        try:  # Reading can fail
            typed_line = (
                sys.stdin.readline()
            )  # Wait for a line (blocks only this thread)
        except (OSError, ValueError):  # The input stream is broken
            typed_line = ""  # Treat it like the end of the input
        if not typed_line:  # An empty string means the input has ended
            stdin_state["closed"] = True  # Remember that no more input will come
            return  # End the thread
        stdin_lines.put(item=typed_line)  # Hand the line to the main thread


def start_stdin_reader() -> None:  # Start the keyboard thread once
    if stdin_state["started"]:  # The thread already exists
        return  # Do not start a second one
    stdin_state["started"] = True  # Remember that the thread now exists
    threading.Thread(
        target=stdin_reader_loop, name="stdin-reader", daemon=True
    ).start()  # Daemon: never blocks the exit


def pump_browser(
    *, browser_page: Page
) -> None:  # Let Playwright process events for a short moment
    try:  # The page may be closed
        browser_page.wait_for_timeout(
            timeout=KEYBOARD_POLL_MS
        )  # Let Playwright process events
    except PlaywrightError:  # The page is closed or the browser is gone
        time.sleep(KEYBOARD_POLL_MS / 1000)  # Just sleep instead


def take_typed_line() -> (
    bool
):  # Remove one typed line from the queue; True if there was one
    try:  # The queue may be empty
        stdin_lines.get_nowait()  # Take one line
    except queue.Empty:  # Nothing typed
        return False  # No line
    return True  # A line was typed


def wait_for_enter(
    *, browser_page: Page, message: str
) -> (
    None
):  # Block until the user presses Enter (keeps the browser alive, nags every few minutes)
    start_stdin_reader()  # Make sure the keyboard thread runs
    while (
        take_typed_line()
    ):  # Throw away Enter presses that happened before this prompt
        pass  # Keep clearing
    totals.waiting_for_user = True  # Tell the heartbeat to stay quiet
    print(message, flush=True)  # Show the prompt
    notify_user(text=message.strip())  # Ring the bell and call the optional webhook
    last_reminder_at = time.monotonic()  # When the prompt was last shown
    try:  # The flag must always be reset
        while True:  # Wait for Enter
            if take_typed_line():  # Enter was pressed
                return  # Leave the function
            if stdin_state[
                "closed"
            ]:  # The terminal input has ended, so nobody can press Enter
                logger.warning(
                    msg=f"Terminal input is closed; continuing automatically in {CLOSED_INPUT_WAIT_SECONDS}s"
                )  # Explain
                time.sleep(CLOSED_INPUT_WAIT_SECONDS)  # Give the browser some time
                return  # Continue as if Enter was pressed
            if (
                time.monotonic() - last_reminder_at >= PROMPT_REPEAT_SECONDS
            ):  # Nobody answered for a while
                print(
                    f"\n(still waiting for you){message}", flush=True
                )  # Show the prompt again
                notify_user(text=message.strip())  # Signal again
                last_reminder_at = time.monotonic()  # Restart the reminder timer
            pump_browser(browser_page=browser_page)  # Let Playwright process events
    finally:  # Always runs
        totals.waiting_for_user = False  # The prompt is over


# ===========================================================================
# WALKING THROUGH THE RESULTS PAGES
# ===========================================================================
def find_results_page(
    *, browser_context: BrowserContext
) -> Page | None:  # Find the tab that shows the results grid
    for open_page in browser_context.pages:  # Every open tab
        try:  # A tab can close while we look at it
            if (
                open_page.locator(selector=RESULTS_GRID_SELECTOR).count() > 0
            ):  # The grid exists on this tab
                return open_page  # This is the results tab
        except PlaywrightError:  # This tab cannot be inspected
            continue  # Look at the next tab
    return None  # No tab shows the results grid


def open_results_grid(
    *, browser_context: BrowserContext
) -> Page:  # Wait for the user to reach the search results (no extra requests)
    browser_page = get_working_page(browser_context=browser_context)  # The first tab
    try:  # The start page may not load
        browser_page.goto(
            url=START_PAGE_URL, timeout=REQUEST_TIMEOUT_MS
        )  # Go to the start page
    except PlaywrightError as error:  # The page did not load
        logger.warning(msg=f"Could not open the start page: {error}")  # Tell the user
    message = START_INSTRUCTIONS  # The first prompt
    while True:  # Until the grid is found
        wait_for_enter(browser_page=browser_page, message=message)  # Wait for the user
        if (
            results_page := find_results_page(browser_context=browser_context)
        ) is not None:  # Look for the grid on every tab (the grid only exists after a log-in)
            logger.info(msg="Found the results grid")  # Log the success
            return results_page  # Hand the results tab back
        open_pages = [
            open_page
            for open_page in browser_context.pages
            if not open_page.is_closed()
        ]  # Every open tab
        logged_in = any(
            page_shows_logout_link(page=open_page) for open_page in open_pages
        )  # Does any tab show the logout link?
        message = (
            NO_RESULTS_REMINDER if logged_in else NOT_LOGGED_IN_REMINDER
        )  # Ask for the right next step


def read_page_numbers(
    *, browser_page: Page
) -> tuple[int | None, int | None]:  # Read the current and total page numbers
    try:  # The label can be missing or slow
        label_text = (
            browser_page.locator(selector=PAGE_LABEL_SELECTOR).text_content(
                timeout=10_000
            )
            or ""
        )  # The label text
    except PlaywrightError:  # The label could not be read
        return None, None  # Report "unknown"
    match_found = PAGE_LABEL_PATTERN.search(
        string=label_text.replace(NON_BREAKING_SPACE, " ")
    )  # Find the page numbers
    if not match_found:  # The text has an unexpected shape
        return None, None  # Report "unknown"
    return int(match_found.group("current")), int(
        match_found.group("total")
    )  # The numbers as integers


def read_first_ident(
    *, browser_page: Page
) -> str:  # Read the internal ID of the first document in the grid ('' if unknown)
    try:  # The page may be busy
        return str(
            browser_page.evaluate(expression=FIRST_IDENT_JAVASCRIPT) or ""
        )  # Run the JavaScript
    except PlaywrightError:  # The page could not be inspected
        return ""  # Report "unknown"


def go_to_next_results_page(
    *, browser_page: Page, next_page_number: int
) -> None:  # Click next and wait until the label AND the grid changed (never gives up)
    previous_ident = read_first_ident(
        browser_page=browser_page
    )  # First document of the old page, to prove the grid was replaced
    round_number = 0  # Rounds of attempts
    while True:  # Until the page changed
        round_number += 1  # Count this round
        if browser_page.is_closed():  # The results tab was closed
            raise BrowserLostError(
                "the results tab was closed"
            )  # The main loop restarts the session
        for attempt_number in range(1, RETRY_ATTEMPTS + 1):  # Several tries per round
            try:  # Clicking or waiting can time out
                browser_page.click(
                    selector=NEXT_PAGE_BUTTON_SELECTOR, timeout=REQUEST_TIMEOUT_MS
                )  # Click the next page button
                browser_page.wait_for_function(
                    expression=PAGE_CHANGE_CHECK_JAVASCRIPT,
                    arg={
                        "pageNumber": next_page_number,
                        "previousIdent": previous_ident,
                    },
                    timeout=REQUEST_TIMEOUT_MS,
                )  # Wait for the label and the rows to change
                return  # Success
            except PlaywrightError as error:  # Timeout or error during click or wait
                logger.warning(
                    msg=f"Going to results page {next_page_number} failed (round {round_number}, try {attempt_number}/{RETRY_ATTEMPTS}): {error}"
                )  # Log it
                current_now, _ = read_page_numbers(
                    browser_page=browser_page
                )  # The click may have worked, just slowly
                if current_now == next_page_number:  # Already on the wanted page
                    return  # Do NOT click again, or a page would be skipped
                time.sleep(
                    SECONDS_PER_RETRY_STEP * attempt_number
                )  # Short pause before the next try
        wait_seconds = min(
            MAXIMUM_PAGING_BACKOFF_SECONDS, SECONDS_PER_RETRY_STEP * 2**round_number
        )  # Longer pauses between rounds
        logger.error(
            msg=f"Paging to results page {next_page_number} keeps failing; trying again in {wait_seconds}s (Ctrl+C is the only way to stop)"
        )  # Explain
        time.sleep(wait_seconds)  # Wait before the next round


def read_grid_records(
    *, grid_page: Page, page_number: int
) -> list[DocumentRecord]:  # Read the documents of the current results page
    for attempt_number in range(1, RETRY_ATTEMPTS + 1):  # Several tries
        try:  # Reading and parsing can fail
            grid_html = grid_page.locator(selector=RESULTS_GRID_SELECTOR).evaluate(
                expression=GET_OUTER_HTML_JAVASCRIPT
            )  # The HTML of the grid
            return parse_results_grid(html_text=grid_html)  # Parse it into records
        except (
            PlaywrightError,
            etree.LxmlError,
            ValueError,
        ) as error:  # Browser or parsing problem
            logger.warning(
                msg=f"Reading results page {page_number} failed (try {attempt_number}/{RETRY_ATTEMPTS}): {error}"
            )  # Log it
            time.sleep(
                SECONDS_PER_RETRY_STEP * attempt_number
            )  # Wait before trying again
    logger.error(
        msg=f"Results page {page_number} could not be read; skipping to the next page"
    )  # Every attempt failed
    return []  # Continue with an empty page instead of stopping


def pause_for_layout_check(
    *, browser_page: Page, problem: str
) -> None:  # Stop and ask the user to look at the website
    logger.error(msg=f"Possible website layout change: {problem}")  # Log the suspicion
    wait_for_enter(
        browser_page=browser_page, message=LAYOUT_CHECK_MESSAGE.format(problem=problem)
    )  # Wait until the user confirmed


def check_for_empty_pages(
    *, grid_page: Page, page_number: int, page_records: list[DocumentRecord]
) -> list[DocumentRecord]:  # Watch for several empty pages in a row
    if page_records:  # The page has documents
        totals.consecutive_empty_pages = 0  # The streak is over
        return page_records  # Nothing to do
    totals.consecutive_empty_pages += 1  # One more empty page in a row
    logger.warning(
        msg=f"Results page {page_number} contains no documents (the site layout may have changed)"
    )  # Warn
    if totals.consecutive_empty_pages < EMPTY_PAGES_BEFORE_ALERT:  # Not enough yet
        return page_records  # Keep going
    pause_for_layout_check(
        browser_page=grid_page,
        problem=f"{totals.consecutive_empty_pages} results pages in a row contain no documents",
    )  # Stop and ask
    totals.consecutive_empty_pages = 0  # Start counting again
    return read_grid_records(
        grid_page=grid_page, page_number=page_number
    )  # Read the page again, the user may have fixed something


def iterate_results_pages(
    *, grid_page: Page
) -> Iterator[
    tuple[int, int, list[DocumentRecord]]
]:  # Yield the documents page by page
    current_page_number, total_page_count = read_page_numbers(
        browser_page=grid_page
    )  # Read the first page numbers
    while (
        current_page_number is None or total_page_count is None
    ):  # The page counter cannot be read
        logger.error(
            msg="Could not read 'Page X of Y' from the results page."
        )  # Explain
        wait_for_enter(
            browser_page=grid_page, message=NO_RESULTS_REMINDER
        )  # Ask the user to fix the browser
        current_page_number, total_page_count = read_page_numbers(
            browser_page=grid_page
        )  # Try again
    logger.info(
        msg=f"Starting at results page {current_page_number} of {total_page_count}"
    )  # Log where we start
    while True:  # Over all pages
        page_records = read_grid_records(
            grid_page=grid_page, page_number=current_page_number
        )  # The documents of this page
        page_records = check_for_empty_pages(
            grid_page=grid_page,
            page_number=current_page_number,
            page_records=page_records,
        )  # Layout check
        yield current_page_number, total_page_count, page_records  # Hand this page to the caller
        if current_page_number >= total_page_count:  # The last page was reached
            return  # End the generator
        go_to_next_results_page(
            browser_page=grid_page, next_page_number=current_page_number + 1
        )  # Navigate to the next page
        current_page_number += 1  # Count the page
        _, latest_total = read_page_numbers(
            browser_page=grid_page
        )  # The site may have changed the page total
        if latest_total is not None:  # The total could be read
            total_page_count = latest_total  # Use the fresh total
        polite_pause()  # Wait briefly to be polite to the server


# ===========================================================================
# PROBLEMS, TOTALS AND SUMMARIES
# ===========================================================================
def record_failure(
    *, document_label: str, url: str, reason: str
) -> None:  # Count a problem and remember it in memory
    totals.failed += 1  # Count the failure
    timestamp = datetime.now().strftime(LOG_DATE_FORMAT)  # Create a timestamp
    totals.failures.append(
        FailureRecord(timestamp=timestamp, label=document_label, url=url, reason=reason)
    )  # Remember it for the final summary
    logger.error(msg=f"{document_label}: FAILED - {reason}")  # Log the failure
    if totals.failed == 1:  # The very first problem of the run
        logger.info(
            msg="First problem recorded; every problem is listed again at the end of the run"
        )  # Tell the user


def totals_summary() -> str:  # Build the one-line summary of the running totals
    elapsed_seconds = time.monotonic() - totals.started_at  # Seconds since the start
    documents_per_minute = (
        totals.documents / (elapsed_seconds / 60)
        if elapsed_seconds >= 60
        else float(totals.documents)
    )  # Average speed (plain count in the first minute)
    return (  # Build the text
        f"{totals.documents} documents ({documents_per_minute:.1f}/min), {totals.saved} PDFs saved "  # Documents and PDFs
        f"({totals.bytes_saved / BYTES_PER_MEGABYTE:.1f} MB), {totals.skipped} skipped, "  # Size and skips
        f"{len(totals.new_documents)} new IDs, {len(totals.updated_documents)} known IDs with new files, "  # Changes
        f"{totals.no_pdfs} without PDFs, {totals.suspect} suspect, {len(totals.refused_files)} refused, {totals.failed} problems, "  # Oddities
        f"{totals.recovered} recovered, {totals.restarts} restarts, elapsed {format_duration(total_seconds=elapsed_seconds)}"  # Health and time
    )  # End of the text


def log_totals() -> None:  # Print the current running totals
    logger.info(msg=f"Totals: {totals_summary()}")  # One summary line


def log_failure_summary() -> None:  # List the problems at the end of the run
    if not totals.failures:  # Nothing went wrong
        return  # Nothing to list
    logger.warning(
        msg=f"{len(totals.failures)} problems were recorded, {len(totals.failed_documents)} documents still have problems"
    )  # The big picture
    shown_failures = sorted(
        totals.failures[-MAXIMUM_FAILURES_SHOWN:], key=lambda failure: failure.label
    )  # The most recent problems, sorted by label
    for failure in shown_failures:  # Print each problem
        logger.warning(
            msg=f"  {failure.timestamp} | {failure.label} | {failure.reason} | {failure.url}"
        )  # One line per problem
    hidden_count = len(totals.failures) - len(shown_failures)  # Problems not listed
    if hidden_count > 0:  # The list was cut short
        logger.warning(
            msg=f"  ... and {hidden_count} older problems not shown"
        )  # Say so


def log_change_summary() -> (
    None
):  # List the new IDs and the new files of known IDs found in this run
    logger.info(
        msg=f"Changes in this run: {len(totals.new_documents)} new IDs, {len(totals.updated_documents)} known IDs received new files"
    )  # The big picture
    for ident, doc_id in list(totals.new_documents.items())[
        :MAXIMUM_FAILURES_SHOWN
    ]:  # Each new ID
        logger.info(msg=f"  new ID {ident} ({doc_id})")  # One line per ID
    for ident, dmxids in list(totals.updated_documents.items())[
        :MAXIMUM_FAILURES_SHOWN
    ]:  # Each known ID with new files
        logger.info(
            msg=f"  known ID {ident}: new files {', '.join(dmxids)}"
        )  # One line per ID


def log_refused_summary() -> None:  # List the files the server refused to send
    if not totals.refused_files:  # Nothing was refused
        return  # Nothing to list
    logger.warning(
        msg=f"{len(totals.refused_files)} files were refused by the server (not requested again for {REFUSED_RECHECK_HOURS} hours):"
    )  # The big picture
    for refused_file in totals.refused_files[
        :MAXIMUM_FAILURES_SHOWN
    ]:  # Print each file
        logger.warning(msg=f"  {refused_file}")  # One line per file
    hidden_count = (
        len(totals.refused_files) - MAXIMUM_FAILURES_SHOWN
    )  # Files not listed
    if hidden_count > 0:  # The list was cut short
        logger.warning(
            msg=f"  ... and {hidden_count} more (see the 'refused' table in {STATE_DATABASE_FILE})"
        )  # Say so


def log_suspect_summary() -> None:  # List the PDFs that still look incomplete
    if not totals.suspect_files:  # Every file looked complete
        return  # Nothing to list
    logger.warning(
        msg=f"{len(totals.suspect_files)} PDFs look incomplete (no %%EOF marker); they were saved with '{SUSPECT_MARKER}' in the name:"
    )  # The big picture
    for suspect_file in totals.suspect_files[
        :MAXIMUM_FAILURES_SHOWN
    ]:  # Print each file
        logger.warning(msg=f"  {suspect_file}")  # One line per file
    hidden_count = (
        len(totals.suspect_files) - MAXIMUM_FAILURES_SHOWN
    )  # Files not listed
    if hidden_count > 0:  # The list was cut short
        logger.warning(
            msg=f"  ... and {hidden_count} more (see {STATE_DATABASE_FILE})"
        )  # Say so


# ===========================================================================
# DOWNLOADING THE PDFS OF A DOCUMENT
# ===========================================================================
def existing_file_is_valid(
    *, target_path: Path
) -> bool:  # Check that an already downloaded file is plausible
    try:  # The file system can fail
        if target_path.stat().st_size < MINIMUM_PDF_BYTES:  # A tiny file is broken
            return False  # Download it again
        with target_path.open(mode="rb") as existing_file:  # Open the file
            start_of_file = existing_file.read(
                PDF_SIGNATURE_SEARCH_BYTES
            )  # Read the beginning
        return PDF_SIGNATURE in start_of_file  # A real PDF starts with the signature
    except OSError:  # The file cannot be read
        return False  # Treat it as broken


def should_skip_existing_file(
    *,
    document_record: DocumentRecord,
    pdf_link: PdfLink,
    file_label: str,
    target_path: Path,
    suspect_path: Path,
) -> bool:  # Skip a PDF that is already saved (no request is made for it)
    recorded_path = lookup_saved_path(
        ident=document_record["ident"], dmxid=pdf_link["dmxid"]
    )  # Where the database says the file is
    for candidate_path in (
        recorded_path,
        target_path,
        suspect_path,
    ):  # The recorded path first, then the expected names
        if candidate_path is None or not candidate_path.exists():  # Nothing there
            continue  # Try the next candidate
        if not existing_file_is_valid(target_path=candidate_path):  # The file is broken
            logger.warning(
                msg=f"{file_label}: the existing file {candidate_path.name} looks broken, downloading it again"
            )  # Explain
            continue  # Try the next candidate
        logger.info(
            msg=f"{file_label}: already downloaded ({candidate_path.name}), skipping"
        )  # Log the skip
        totals.skipped += 1  # Count it
        if recorded_path is None:  # The database does not know this file yet
            remember_file_from_disk(
                document_record=document_record,
                pdf_link=pdf_link,
                saved_path=candidate_path,
            )  # Add it
        return True  # Skip
    return False  # Nothing usable on disk: download it


def record_save_failure(
    *, file_label: str, target_path: Path, error: OSError
) -> None:  # Record a disk problem while saving
    match error.errno:  # Look at the operating system error number
        case errno.ENOSPC:  # The disk is full
            record_failure(
                document_label=file_label,
                url=str(target_path),
                reason="the disk was full (will be retried)",
            )  # Remember it
            wait_for_disk_space()  # Wait until space was freed instead of stopping
        case _:  # Any other file problem
            record_failure(
                document_label=file_label,
                url=str(target_path),
                reason=f"could not save the file: {error}",
            )  # Remember it


def register_saved_pdf(
    *,
    document_record: DocumentRecord,
    pdf_link: PdfLink,
    file_label: str,
    final_path: Path,
    download: PdfDownload,
    is_suspect: bool,
    document_is_new: bool,
) -> None:  # Update the totals and the database after a PDF was saved
    if is_suspect:  # The file looks incomplete
        totals.suspect += 1  # Count it
        totals.suspect_files.append(str(final_path))  # Remember which file it is
        logger.warning(
            msg=f"{file_label}: no %%EOF end marker, the file may be incomplete; saved as {final_path.name}"
        )  # Warn
    logger.info(
        msg=f"{file_label}: SAVED -> {final_path} ({download.size} bytes, sha256 {download.sha256[:12]})"
    )  # Log the success
    totals.saved += 1  # Count the file
    totals.bytes_saved += download.size  # Add to the byte counter
    record_file(
        document_record=document_record,
        pdf_link=pdf_link,
        saved_path=final_path,
        download=download,
        suspect=is_suspect,
    )  # Remember it in the database
    note_new_file(
        document_record=document_record,
        pdf_link=pdf_link,
        document_is_new=document_is_new,
    )  # Announce it if the ID was already known


def download_one_pdf(
    *,
    browser_context: BrowserContext,
    document_record: DocumentRecord,
    pdf_link: PdfLink,
    file_label: str,
    document_folder: Path,
    detail_url: str,
    document_is_new: bool,
) -> bool:  # Download and save one PDF; True if it worked
    target_path = document_folder / build_file_name(
        document_folder=document_folder, pdf_link=pdf_link
    )  # Final path
    suspect_path = suspect_path_for(
        target_path=target_path
    )  # Path used if the file looks incomplete
    partial_path = target_path.with_name(
        target_path.name + PARTIAL_FILE_EXTENSION
    )  # Temporary file
    if should_skip_existing_file(
        document_record=document_record,
        pdf_link=pdf_link,
        file_label=file_label,
        target_path=target_path,
        suspect_path=suspect_path,
    ):  # Already saved earlier
        return True  # Nothing to do, and no request was made
    if refused_recently(
        ident=document_record["ident"], dmxid=pdf_link["dmxid"]
    ):  # The server refused this file recently
        logger.info(
            msg=f"{file_label}: the server refused this file within the last {REFUSED_RECHECK_HOURS} hours, not asking again"
        )  # Log the skip
        return True  # Nothing to do, and no request was made
    try:  # Downloading and saving can fail
        download = fetch_pdf_with_retries(
            browser_context=browser_context,
            url=pdf_link["url"],
            label=file_label,
            detail_url=detail_url,
            partial_path=partial_path,
        )  # Let Chrome download the PDF (one request)
        is_suspect = (
            not download.has_end_marker
        )  # Complete PDFs normally end with %%EOF
        final_path = (
            suspect_path if is_suspect else target_path
        )  # Suspect files get a different name
        download.partial_path.replace(
            final_path
        )  # Rename the finished file in one atomic step
    except RefusedDownloadError as error:  # The server refused to send the file
        record_refused_file(
            document_record=document_record, pdf_link=pdf_link, reason=str(error)
        )  # Remember it, so it is not requested again soon
        totals.refused_files.append(
            f"{document_record['ident']}/{pdf_link['dmxid']}"
        )  # Remember it for the final summary
        logger.warning(msg=str(error))  # Tell the user
        return True  # Not a problem of this run: it is listed in the summary
    except PermanentFetchError as error:  # The download failed for good
        record_failure(
            document_label=file_label, url=pdf_link["url"], reason=str(error)
        )  # Remember it
        return False  # This file failed
    except OSError as error:  # Saving to disk failed
        record_save_failure(
            file_label=file_label, target_path=target_path, error=error
        )  # Remember it
        return False  # This file failed
    except Exception as error:  # Any other surprise must not stop the run either
        record_failure(
            document_label=file_label,
            url=pdf_link["url"],
            reason=f"unexpected error: {error!r}",
        )  # Remember it
        return False  # This file failed
    else:  # Download and saving both worked
        register_saved_pdf(
            document_record=document_record,
            pdf_link=pdf_link,
            file_label=file_label,
            final_path=final_path,
            download=download,
            is_suspect=is_suspect,
            document_is_new=document_is_new,
        )  # Update totals and database
        return True  # This file worked
    finally:  # Always runs
        unlink_quietly(
            file_path=partial_path
        )  # The temporary file must never stay behind
        polite_pause()  # Wait briefly between file downloads


def load_pdf_links(
    *,
    browser_context: BrowserContext,
    document_record: DocumentRecord,
    document_label: str,
) -> (
    tuple[str, list[PdfLink]] | None
):  # Let Chrome visit a document page and read its PDF links (None on failure)
    detail_request_url = DETAIL_PAGE_URL_TEMPLATE.format(
        document_record["ident"]
    )  # Address of the document page
    try:  # Fetching can fail
        detail_url, detail_body = fetch_page_with_retries(
            browser_context=browser_context,
            url=detail_request_url,
            label=f"{document_label} document page",
            validate_body=validate_detail_page,
        )  # Let Chrome visit and validate the page
    except PermanentFetchError as error:  # Every attempt failed
        record_failure(
            document_label=document_label, url=detail_request_url, reason=str(error)
        )  # Remember it
        return None  # The document failed
    try:  # HTML parsing can fail on odd pages
        return detail_url, parse_detail_page(
            html_text=decode_body(response_body=detail_body), page_url=detail_url
        )  # The final URL and the links
    except (etree.LxmlError, ValueError) as error:  # Parsing problem
        record_failure(
            document_label=document_label,
            url=detail_url,
            reason=f"could not parse the document page: {error}",
        )  # Remember it
        return None  # The document failed


def download_one_document(
    *,
    browser_context: BrowserContext,
    document_record: DocumentRecord,
    document_label: str,
) -> (
    bool
):  # Check one ID: read its page, download only the files not saved yet; True if all worked
    totals.documents += 1  # Count this document
    loaded = load_pdf_links(
        browser_context=browser_context,
        document_record=document_record,
        document_label=document_label,
    )  # The document page and its links (one request)
    if loaded is None:  # Loading failed
        return False  # The document failed
    detail_url, pdf_links = loaded  # Unpack the result
    ident = document_record["ident"]  # Internal document ID
    document_is_new = ident in totals.new_documents or not document_is_known(
        ident=ident
    )  # Did no earlier run see this ID?
    if document_is_new:  # A new ID
        note_new_document(document_record=document_record)  # Announce it
    if not pdf_links:  # The page lists no PDF files
        totals.no_pdfs += 1  # Count it
        totals.consecutive_no_pdfs += 1  # One more document without PDFs in a row
        logger.warning(
            msg=f"{document_label}: no PDF links found on the document page"
        )  # May be normal or a layout change
        record_document_check(
            document_record=document_record, pdf_count=0
        )  # Remember that it was checked
        return True  # Not an error in itself
    totals.consecutive_no_pdfs = 0  # The streak is over
    document_folder = PDF_FOLDER / build_folder_name(
        document_record=document_record
    )  # The folder of this document
    document_ok = True  # Whether every file of this document worked
    for file_position, pdf_link in enumerate(
        pdf_links, start=1
    ):  # Every PDF listed on the page
        file_ok = download_one_pdf(
            browser_context=browser_context,
            document_record=document_record,
            pdf_link=pdf_link,
            file_label=f"{document_label} file {file_position}/{len(pdf_links)}",
            document_folder=document_folder,
            detail_url=detail_url,
            document_is_new=document_is_new,
        )  # Download it unless it is already saved
        document_ok = document_ok and file_ok  # One failure fails the document
    if document_ok:  # Every file is saved
        record_document_check(
            document_record=document_record, pdf_count=len(pdf_links)
        )  # Remember that the ID was checked now
    return document_ok  # Report the result


def process_document(
    *,
    browser_context: BrowserContext,
    document_record: DocumentRecord,
    document_label: str,
    page_number: int,
    log_suffix: str,
) -> bool:  # Check one document and sort it into "done" or "still failing"
    ident = document_record["ident"]  # Internal document ID
    log_label = (
        document_label + log_suffix
    )  # The label shown in the log (the stored label never gets suffixes)
    try:  # NO error may escape from a single document
        document_ok = download_one_document(
            browser_context=browser_context,
            document_record=document_record,
            document_label=log_label,
        )  # Check the document
    except Exception as error:  # Anything can go wrong
        record_failure(
            document_label=log_label,
            url=DETAIL_PAGE_URL_TEMPLATE.format(ident),
            reason=f"unexpected error: {error!r}",
        )  # Remember it
        document_ok = False  # This document failed
    if document_ok:  # Every file of the document worked
        if ident in totals.failed_documents:  # It failed earlier in this run
            del totals.failed_documents[ident]  # It is not a problem any more
            totals.recovered += 1  # Count the recovery
    else:  # Something went wrong
        totals.failed_documents[ident] = FailedDocument(
            record=document_record, label=document_label, page_number=page_number
        )  # Remember it for the retry round
    circuit_breaker(
        browser_context=browser_context, document_ok=document_ok
    )  # Pause if too many documents fail in a row
    return document_ok  # Report the result


def retry_failed_documents(
    *, browser_context: BrowserContext
) -> None:  # Give failed documents another chance after the main run
    for round_number in range(1, FINAL_RETRY_ROUNDS + 1):  # A few rounds
        if not totals.failed_documents:  # Nothing left to retry
            return  # Done
        logger.info(
            msg=f"=== Retry round {round_number}/{FINAL_RETRY_ROUNDS}: {len(totals.failed_documents)} documents ==="
        )  # Announce the round
        for failed_document in list(
            totals.failed_documents.values()
        ):  # Work on a copy because the dictionary changes
            process_document(
                browser_context=browser_context,
                document_record=failed_document.record,
                document_label=failed_document.label,
                page_number=failed_document.page_number,
                log_suffix=f" (retry {round_number})",
            )  # Try again (the suffix is only for logging)
            polite_pause()  # Wait briefly between documents
    if totals.failed_documents:  # Some documents still fail
        logger.warning(
            msg=f"{len(totals.failed_documents)} documents still fail after {FINAL_RETRY_ROUNDS} retry rounds"
        )  # Say so


def check_no_pdf_streak(
    *, grid_page: Page
) -> None:  # Pause when many documents in a row have no PDF links
    if totals.consecutive_no_pdfs < totals.no_pdf_threshold:  # Not enough yet
        return  # Keep going
    pause_for_layout_check(
        browser_page=grid_page,
        problem=f"{totals.consecutive_no_pdfs} documents in a row have no PDF links",
    )  # Stop and ask
    totals.consecutive_no_pdfs = 0  # Start counting again
    totals.no_pdf_threshold *= (
        NO_PDF_STREAK_GROWTH_FACTOR  # You said it is fine, so be less nervous next time
    )


def process_results_page(
    *,
    browser_context: BrowserContext,
    grid_page: Page,
    page_number: int,
    page_records: list[DocumentRecord],
    seen_idents: set[str],
) -> int:  # Check every document of one results page; returns how many were processed
    processed_count = 0  # Documents processed on this page
    for position, document_record in enumerate(
        page_records, start=1
    ):  # Every document of the page
        if grid_page.is_closed():  # The results tab disappeared
            raise BrowserLostError(
                "the results tab was closed"
            )  # The main loop restarts the session
        ident = document_record["ident"]  # Internal document ID
        if ident in seen_idents:  # Already checked in this run (no second request)
            logger.info(
                msg=f"Document {ident} already checked in this run, skipping"
            )  # Log the skip
            continue  # Next document
        seen_idents.add(ident)  # Remember this document
        document_label = f"[p{page_number} {position}/{len(page_records)}] {document_record['doc_id'] or ident}"  # Label for the log
        process_document(
            browser_context=browser_context,
            document_record=document_record,
            document_label=document_label,
            page_number=page_number,
            log_suffix="",
        )  # Check it (errors are handled inside)
        check_no_pdf_streak(grid_page=grid_page)  # Layout check
        processed_count += 1  # Count it
        if processed_count % PROGRESS_EVERY_N_DOCUMENTS == 0:  # A batch boundary
            log_totals()  # Print the totals
    return processed_count  # Report how many were processed


def download_all_documents(
    *, browser_context: BrowserContext
) -> None:  # Control the whole downloading process
    grid_page = open_results_grid(
        browser_context=browser_context
    )  # Wait until the results grid is shown
    seen_idents: set[str] = (
        set()
    )  # Documents seen in this session (a document on two pages is done once)
    for page_number, total_page_count, page_records in iterate_results_pages(
        grid_page=grid_page
    ):  # Every results page
        logger.info(
            msg=f"=== Results page {page_number}/{total_page_count}: {len(page_records)} documents ==="
        )  # Log the page
        process_results_page(
            browser_context=browser_context,
            grid_page=grid_page,
            page_number=page_number,
            page_records=page_records,
            seen_idents=seen_idents,
        )  # Download its documents
    retry_failed_documents(
        browser_context=browser_context
    )  # Give every failed document another chance


# ===========================================================================
# MAIN PROGRAM
# ===========================================================================
def run_session(
    *, playwright_instance: Playwright
) -> bool:  # Run one complete browser session
    browser_context = open_browser_context(
        playwright_instance=playwright_instance
    )  # The configured browser
    try:  # The browser must always be closed
        download_all_documents(
            browser_context=browser_context
        )  # Start the main process
        return True  # The whole run finished normally
    finally:  # Runs no matter what
        logger.info(msg="Closing the browser")  # Log the shutdown
        try:  # Closing can fail
            browser_context.close()  # Close the browser
        except PlaywrightError:  # The browser is already gone
            pass  # Nothing more to do


def run_until_finished() -> None:  # Keep starting browser sessions until one finishes
    finished = False  # Becomes True when the whole run completed
    while not finished:  # Until a session finishes
        try:  # One session
            with sync_playwright() as playwright_instance:  # Start Playwright
                finished = run_session(
                    playwright_instance=playwright_instance
                )  # Run one session
        except (
            Exception
        ):  # Any crash of the session (browser closed, Chromium missing, ...)
            totals.restarts += 1  # Count the restart
            wait_seconds = min(
                MAXIMUM_RESTART_WAIT_SECONDS, RESTART_WAIT_SECONDS * totals.restarts
            )  # Wait longer after every crash
            logger.exception(
                msg=f"The session crashed; restarting in {wait_seconds}s (finished documents and files are skipped)"
            )  # Log the traceback
            time.sleep(wait_seconds)  # Wait before the next session


def main() -> int:  # The entry point; returns the exit code
    setup_logging()  # Start logging first, so everything below is recorded
    exit_code = 0  # Assume success until something goes wrong
    try:  # The only way to stop the script is Ctrl+C
        run_preflight_checks()  # Check terminal, write access and disk space
        log_database_overview()  # Tell what earlier runs already know
        start_heartbeat_thread()  # Start the "still running" messages
        run_until_finished()  # Run until everything is done
    except KeyboardInterrupt:  # The user pressed Ctrl+C
        logger.warning(
            msg="Stopped by you (Ctrl+C). Run again to continue; finished documents and files are skipped."
        )  # Log it
        exit_code = 130  # The conventional exit code for Ctrl+C
    finally:  # Runs before exiting
        heartbeat_stop.set()  # Stop the heartbeat thread
        close_database()  # Close the local database
        log_totals()  # Print the final totals
        log_change_summary()  # List the new IDs and the new files of known IDs
        log_suspect_summary()  # List the PDFs that look incomplete
        log_refused_summary()  # List the files the server refused
        log_failure_summary()  # List the problems
        if totals.failed_documents:  # Some documents still have problems
            exit_code = exit_code or 1  # Make sure the exit code reports them
    return exit_code  # Hand the exit code back


if __name__ == "__main__":  # Only when the script is run directly
    sys.exit(main())  # Run main and pass its exit code to the operating system

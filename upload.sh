#!/bin/bash
# The shebang above must stand alone on its own line; a trailing comment would be passed to bash as a bogus argument.

# -----------------------------------------------------------------------------
# Auto Git Sync Script, production edition (strict: exits on ANY error except network errors, which are retried)
# Continuously watches a Git repository and does the following:
# 1. Compresses each new or modified PDF (smallest first) as soon as it is found, then stages it.
# 2. Uses the Git index (staging area) as the "already compressed" marker.
# 3. Pushes as soon as the FIRST of these happens: 100 files are staged, about 1 GB is staged, or 12 hours have
#    passed since the last push (all three limits are settings, see the option table below).
# 4. Commits BEFORE pulling, so the batch is a real commit rather than fragile staged state.
# 5. Never stages a PDF that was not compressed by this script.
# 6. Exits immediately if any command fails (run it under systemd with Restart=on-failure),
#    EXCEPT network failures: a failed git pull, git push or git ls-remote is retried (see item 11).
# 7. Shows EVERYTHING on the console only (no log file): this script, git, Ghostscript (page by page,
#    labelled with the PDF being compressed) and, optionally, the logs of other apps such as the downloader.
# 8. Ignores the downloader's half-finished '*.part' files and Chrome's temporary files and browser profile, so they
#    can never be committed (see the ignore list in main).
# 9. Protects production: timeouts on every external command, a free-disk check, a startup remote check,
#    a periodic heartbeat, running statistics, and a final summary on exit.
# 10. Only compresses PDFs that are completely downloaded: a PDF is skipped while a '<name>.pdf.part' file exists,
#     while it was modified less than PDF_SETTLE_SECONDS ago, while another process still has it open, or while it
#     does not start with the PDF signature. It is checked again right before compression, and the compressed copy is
#     thrown away if the PDF changed while Ghostscript was running, so a re-download is never overwritten.
# 11. Retries network problems: if the pull or the push fails, the WHOLE pull + push sequence is repeated after a
#     wait that doubles each time (NETWORK_RETRY_DELAY_SECONDS up to NETWORK_RETRY_MAX_DELAY_SECONDS). A real rebase
#     conflict is NOT a network problem, so the script aborts the rebase and exits instead.
# The script takes NO command-line arguments: just run  bash upload.sh  (an argument is rejected with a clear message).
# Every setting has a default and can be changed with an environment variable, e.g.  PDF_SETTLE_SECONDS=60 bash upload.sh
# The value in use for EVERY setting, its default and where it came from are printed in the log at startup.
# Everything is logged: every function call, every file operation and every git and Ghostscript line.
# For a literal trace of every command as well: SHELL_TRACE=1 bash upload.sh
# To merge the downloader's log into this one: EXTRA_LOG_FILES=/var/log/etsi-downloader.log bash upload.sh
# -----------------------------------------------------------------------------

set -Eeuo pipefail # Exit on errors, inherit the ERR trap in functions, fail on unset variables, fail on pipeline errors

readonly SCRIPT_VERSION="2.7.0"                                                                                  # Version string shown in the startup report
export LC_ALL=C                                                                                                  # Use plain byte-wise text handling so file names never confuse sort or grep
export GIT_TERMINAL_PROMPT=0                                                                                     # Never let git wait for a password on a terminal nobody is watching
export GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh -o BatchMode=yes -o ConnectTimeout=30 -o ServerAliveInterval=30}" # Make SSH fail fast instead of hanging or prompting

# ---------------- Options (priority: environment variable > default; the script takes NO arguments) ----------------

if ((BASH_VERSINFO[0] < 4 || (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] < 4))); then # Associative arrays and mapfile -d need Bash 4.4 or newer
	printf 'upload.sh: Bash 4.4 or newer is required (found %s).\n' "$BASH_VERSION" >&2 # Say so before anything else can fail in a confusing way
	exit 1                                                                              # Stop at once
fi                                                                                   # End of the Bash version check

# The ONE table that defines every setting. Each entry is: ENVIRONMENT_VARIABLE|default|kind|meaning
# kinds: int1 = whole number >= 1, int0 = whole number >= 0, bool = 0 or 1, level = 0 to 4, quality = Ghostscript preset, text = any text
declare -ra OPTION_DEFINITIONS=( # Start of the read-only table of settings
	"REPOSITORY_DIRECTORY|auto|text|Repository to watch (auto = the current directory)"                                            # Which repository to watch
	"CHECK_INTERVAL_SECONDS|5|int1|Seconds to sleep between repository checks"                                                     # Pause between cycles
	"MAX_BATCH_SIZE|100|int1|Maximum number of files in one commit/push"                                                           # File-count push limit
	"MAX_BATCH_BYTES|1000000000|int1|Stop adding PDFs to a batch at about this many bytes (GitHub rejects pushes over about 2 GB)" # Byte-size push limit
	"MAX_SECONDS_BETWEEN_PUSHES|43200|int1|Force a push after this many seconds (43200 = 12 hours)"                                # Time push limit
	"GITHUB_MAX_FILE_BYTES|104857600|int1|PDFs larger than this are ignored by Git (GitHub rejects files over 100 MiB)"            # Per-file size limit
	"GHOSTSCRIPT_QUALITY|/ebook|quality|Ghostscript preset: /screen, /ebook, /printer, /prepress or /default"                      # Compression quality
	"GHOSTSCRIPT_TIMEOUT_SECONDS|21600|int1|Kill Ghostscript when one PDF takes longer than this many seconds"                     # Ghostscript time limit
	"GIT_LOCAL_TIMEOUT_SECONDS|300|int1|Time limit in seconds for local git commands (add, commit)"                                # Local git time limit
	"GIT_NETWORK_TIMEOUT_SECONDS|3600|int1|Time limit in seconds for git commands that use the network (pull, push)"               # Network git time limit
	"NETWORK_RETRY_DELAY_SECONDS|30|int1|Seconds to wait before the first retry of a failed network operation (doubles each time)" # First retry wait
	"NETWORK_RETRY_MAX_DELAY_SECONDS|600|int1|The wait between retries never grows beyond this many seconds"                       # Retry wait cap
	"NETWORK_MAX_ATTEMPTS|0|int0|Give up (exit) after this many failed attempts in a row (0 = retry forever)"                      # Retry attempt limit
	"MIN_FREE_DISK_MB|1024|int0|Exit when free disk space drops below this many megabytes"                                         # Free-disk minimum
	"HEARTBEAT_SECONDS|60|int1|How often to log an 'I am alive' status line"                                                       # Heartbeat interval
	"PDF_SETTLE_SECONDS|30|int0|A PDF must be unchanged for this many seconds before it counts as fully downloaded"                # Download settle time
	"LOG_EVERY_SKIPPED_PDF|1|bool|1 = log one line for every PDF that is skipped, 0 = log only the totals"                         # Per-file skip logging
	"LOG_LEVEL_NUMBER|0|level|Log detail: 0=TRACE (everything), 1=DEBUG, 2=INFO, 3=WARN, 4=FATAL"                                  # Log level
	"SHELL_TRACE|0|bool|1 = also print EVERY shell command with a timestamp (extremely verbose)"                                   # Shell command tracing
	"EXTRA_LOG_FILES||text|Space-separated log files of OTHER apps to merge into this console output"                              # Other apps' logs
	"LOCK_FILE|auto|text|Lock file that prevents two copies running on one repository (auto = derived from the repository path)"   # Single-instance lock
)                                                                                                                               # End of the table of settings

declare -A OPTION_SOURCES=() # Where the final value of each setting came from: environment or default
OPTIONS_APPLIED=0            # Becomes 1 once every setting has its final value (before that nothing is started)

function option_kind_hint() {                                          # Describe in words which values a setting accepts
	case "$1" in                                                          # Look at the kind
	int1) printf 'whole number, 1 or more' ;;                             # Positive number
	int0) printf 'whole number, 0 or more' ;;                             # Non-negative number
	bool) printf '0 or 1' ;;                                              # Switch
	level) printf 'whole number from 0 to 4' ;;                           # Log level
	quality) printf '/screen, /ebook, /printer, /prepress or /default' ;; # Ghostscript preset
	*) printf 'text' ;;                                                   # Anything else
	esac                                                                  # End of the kind lookup
}                                                                      # End of option_kind_hint

function print_option_error() {                                                                                   # Print a settings error and stop (used before logging exists)
	printf 'upload.sh: %s\nFix that environment variable (or unset it to use the default) and run again.\n' "$1" >&2 # Explain the problem and the cure
	exit 2                                                                                                           # Exit code 2 means "wrong usage"
}                                                                                                                 # End of print_option_error

function reject_arguments() {                                                                                                          # The script takes no arguments at all
	if [[ $# -gt 0 ]]; then                                                                                                               # Somebody passed something
		printf 'upload.sh: this script takes no arguments (got: %s).\n' "$*" >&2                                                             # Say what was rejected
		printf 'Just run: bash upload.sh   (change a setting with an environment variable, e.g. PDF_SETTLE_SECONDS=60 bash upload.sh)\n' >&2 # Say how to change settings instead
		exit 2                                                                                                                               # Exit code 2 means "wrong usage"
	fi                                                                                                                                    # End of the argument check
}                                                                                                                                      # End of reject_arguments

function apply_options() {                                                                                # Give every setting its final, validated value (and make it read-only)
	local entry variable default kind description value source hint                                          # Working variables
	for entry in "${OPTION_DEFINITIONS[@]}"; do                                                              # Go through the table in order
		IFS='|' read -r variable default kind description <<<"$entry"                                           # Split the entry into its four fields
		hint=$(option_kind_hint "$kind")                                                                        # The accepted values, for messages
		if [[ -n "${!variable+x}" ]]; then                                                                      # 1) an environment variable is set
			value="${!variable}"                                                                                   # Use it
			source="environment"                                                                                   # Remember where it came from
		else                                                                                                    # 2) the default
			value="$default"                                                                                       # Use it
			source="default"                                                                                       # Remember where it came from
		fi                                                                                                      # End of the priority check
		if [[ $value == "auto" ]]; then                                                                         # "auto" values are worked out here
			case "$variable" in                                                                                    # Which setting?
			REPOSITORY_DIRECTORY) value="$PWD" ;;                                                                  # The current directory
			LOCK_FILE) value="/tmp/auto-sync${REPOSITORY_DIRECTORY//\//_}.lock" ;;                                 # Derived from the repository path (set earlier in this loop)
			esac                                                                                                   # End of the auto lookup
		fi                                                                                                      # End of the auto check
		case "$kind" in                                                                                         # Check the value
		int1 | int0 | level)                                                                                    # Numbers
			if [[ ! $value =~ ^[0-9]+$ ]]; then                                                                    # Not a plain non-negative whole number
				print_option_error "invalid value '${value}' for ${variable} (from ${source}): expected ${hint}"      # Stop with a clear message
			fi                                                                                                     # End of the number check
			value=$((10#$value))                                                                                   # Normalise (so 08 is not read as octal)
			if [[ ($kind == int1 && $value -lt 1) || ($kind == level && $value -gt 4) ]]; then                     # Out of range
				print_option_error "invalid value '${value}' for ${variable} (from ${source}): expected ${hint}"      # Stop with a clear message
			fi                                                                                                     # End of the range check
			;;                                                                                                     # End of the number case
		bool)                                                                                                   # Switches
			if [[ $value != "0" && $value != "1" ]]; then                                                          # Neither 0 nor 1
				print_option_error "invalid value '${value}' for ${variable} (from ${source}): expected ${hint}"      # Stop with a clear message
			fi                                                                                                     # End of the switch check
			;;                                                                                                     # End of the switch case
		quality)                                                                                                # Ghostscript presets
			case "$value" in                                                                                       # Which preset?
			/screen | /ebook | /printer | /prepress | /default) ;;                                                 # All valid
			*) print_option_error "invalid value '${value}' for ${variable} (from ${source}): expected ${hint}" ;; # Stop with a clear message
			esac                                                                                                   # End of the preset check
			;;                                                                                                     # End of the preset case
		esac                                                                                                    # End of the value check
		declare -gr "${variable}=${value}"                                                                      # Create the read-only global variable the rest of the script uses
		OPTION_SOURCES["$variable"]="$source"                                                                   # Remember where the value came from
	done                                                                                                     # End of the table loop
	OPTIONS_APPLIED=1                                                                                        # Every setting now has its final value
}                                                                                                         # End of apply_options

function enable_shell_trace_if_requested() {       # Print every shell command with a timestamp when SHELL_TRACE=1
	log_message TRACE "[call] ${FUNCNAME[0]}($*)" >&2 # Log that this function was called, with its arguments
	if [[ $SHELL_TRACE == "1" ]]; then                # Requested
		PS4='+ \D{%T} ${FUNCNAME[0]:-main}:${LINENO}: '  # Prefix each traced command with the time, the function name and the line number
		set -x                                           # Turn command tracing on
	fi                                                # End of the request check
}                                                  # End of enable_shell_trace_if_requested

function log_all_options() {                                                                                                    # Log EVERY setting with its value, default, source and meaning
	log_message TRACE "[call] ${FUNCNAME[0]}($*)" >&2                                                                              # Log that this function was called, with its arguments
	local entry variable default kind description                                                                                  # Fields of one table entry
	log_message INFO "===== OPTIONS (priority: environment variable > default; the script takes no arguments) ====="               # Start of the list
	for entry in "${OPTION_DEFINITIONS[@]}"; do                                                                                    # Every setting of the table
		IFS='|' read -r variable default kind description <<<"$entry"                                                                 # Split the entry into its four fields
		log_message INFO "OPTION ${variable}='${!variable}' [default '${default}'; from ${OPTION_SOURCES[$variable]}] ${description}" # One line per setting
	done                                                                                                                           # End of the setting loop
	log_message INFO "===== END OF OPTIONS ====="                                                                                  # End of the list
}                                                                                                                               # End of log_all_options

# The option values are created by apply_options at the start of main; until then only the table above exists.

# ---------------- Mutable State ----------------

WORK_DIRECTORY=""          # Private temporary directory for Ghostscript files (created at startup)
FOLLOWER_PIDS=()           # Process IDs of the background jobs that follow other apps' log files
STARTED_EPOCH=0            # Unix time when the main loop started
LAST_PUSH_EPOCH=0          # Unix time of the last successful push (or timer reset)
LAST_HEARTBEAT_EPOCH=0     # Unix time of the last heartbeat line
CYCLE_NUMBER=0             # How many monitoring cycles have run
STAGED_FILE_COUNT=0        # Number of files currently staged, refreshed every loop
STAGED_BYTES=0             # Total size in bytes of the staged files, refreshed every loop
SECONDS_SINCE_LAST_PUSH=0  # Seconds since the last successful push, refreshed every loop
TOTAL_PDFS_COMPRESSED=0    # PDFs where the compressed copy replaced the original
TOTAL_PDFS_KEPT_ORIGINAL=0 # PDFs where compression did not help, so the original was kept
TOTAL_PDFS_OVERSIZED=0     # PDFs still over the GitHub limit, ignored by Git
TOTAL_BYTES_BEFORE=0       # Combined size of all PDFs before compression
TOTAL_BYTES_AFTER=0        # Combined size of all PDFs after compression
TOTAL_PUSHES=0             # Number of successful pushes
TOTAL_FILES_PUSHED=0       # Number of files pushed in total
TOTAL_BYTES_PUSHED=0       # Number of bytes pushed in total

# ---------------- Formatting Helpers ----------------

function format_bytes() {                                                                                                                                                                                                                                                 # Print a byte count in human-readable form, e.g. 12.3 MiB
	awk -v bytes="$1" 'BEGIN { split("B KiB MiB GiB TiB", unit, " "); unit_number = 1; while (bytes >= 1024 && unit_number < 5) { bytes /= 1024; unit_number++ } number_format = (unit_number == 1) ? "%d %s" : "%.1f %s"; printf number_format, bytes, unit[unit_number] }' # Divide by 1024 until the number is small
}                                                                                                                                                                                                                                                                         # End of format_bytes

function format_duration() {                                                                             # Print a number of seconds as e.g. 1h02m03s
	local total_seconds="$1"                                                                                # The duration in seconds
	printf '%dh%02dm%02ds' $((total_seconds / 3600)) $((total_seconds % 3600 / 60)) $((total_seconds % 60)) # Split into hours, minutes and seconds
}                                                                                                        # End of format_duration

function available_megabytes() {                                                                # Print the free space (in MB) of the filesystem holding a path
	df -Pk -- "$1" | awk 'NR == 2 { print int($4 / 1024); found=1 } END { if (!found) print "0" }' # Read available space in KB and convert it to MB; print 0 if no data row is found
}                                                                                               # End of available_megabytes

# ---------------- Logging (console only) ----------------

function log_message() {                                                 # Print a timestamped log line to the console if its level is enabled
	local level_name="$1"                                                   # First argument: the level name (TRACE, DEBUG, INFO, ...)
	local message="$2"                                                      # Second argument: the text to log
	local level_number                                                      # Numeric rank of the level, used for filtering
	case "$level_name" in                                                   # Translate the level name into a number
	TRACE) level_number=0 ;;                                                # Most detailed messages
	DEBUG) level_number=1 ;;                                                # Decision, progress and other apps' output
	WARN) level_number=3 ;;                                                 # Something unusual but not fatal
	FATAL) level_number=4 ;;                                                # The script is about to exit
	*) level_number=2 ;;                                                    # INFO, ACTION, SUCCESS and anything else
	esac                                                                    # End of the level translation
	if [[ $level_number -ge ${LOG_LEVEL_NUMBER:-0} ]]; then                 # Print only if this level is enabled
		printf '%(%Y-%m-%dT%H:%M:%S%z)T [%s] %s\n' -1 "$level_name" "$message" # Print: local timestamp with UTC offset, level, message
	fi                                                                      # End of the level check
}                                                                        # End of log_message

function relay_output_to_log() {                               # Turn every line on standard input into a labelled log line
	local level_name="$1"                                         # Level to log the lines at
	local source_label="$2"                                       # Label that says which program or file the line came from
	local output_line                                             # Holds one line of input
	while IFS= read -r output_line || [[ -n "$output_line" ]]; do # Read every line, including a last line without a newline
		log_message "$level_name" "[${source_label}] ${output_line}" # Log the line with its source label
	done                                                          # End of the line loop
}                                                              # End of relay_output_to_log

function run_logged_command() {                                                                                                                # Run a command with a time limit and log all of its output
	local source_label="$1"                                                                                                                       # Label shown in front of every output line
	local timeout_seconds="$2"                                                                                                                    # Kill the command if it runs longer than this
	shift 2                                                                                                                                       # Everything after the first two arguments is the command itself
	local exit_code=0                                                                                                                             # Exit code of the command (0 unless it fails)
	local started_epoch                                                                                                                           # When the command started
	local elapsed_seconds                                                                                                                         # How long the command ran
	started_epoch=$(date +%s)                                                                                                                     # Record the start time
	log_message DEBUG "[${source_label}] running (limit ${timeout_seconds}s): $*"                                                                 # Log the exact command line
	timeout --kill-after=30 "$timeout_seconds" "$@" 2>&1 | tr '\r' '\n' | relay_output_to_log DEBUG "$source_label" || exit_code=${PIPESTATUS[0]} # Run it, split progress lines, log each line, keep the command's exit code
	elapsed_seconds=$(($(date +%s) - started_epoch))                                                                                              # Work out how long it took
	if [[ $exit_code -eq 124 || $exit_code -eq 137 ]]; then                                                                                       # 124/137 mean the time limit killed the command
		log_message WARN "[${source_label}] TIMED OUT after ${timeout_seconds}s"                                                                     # Say so loudly
	fi                                                                                                                                            # End of the timeout check
	log_message DEBUG "[${source_label}] finished with exit code ${exit_code} in ${elapsed_seconds}s"                                             # Log the outcome and duration
	return "$exit_code"                                                                                                                           # Hand the exit code back to the caller
}                                                                                                                                              # End of run_logged_command

# ---------------- Network Retry Helpers ----------------

function wait_before_retry() {                           # Sleep before a retry in a way that a stop signal interrupts immediately
	log_message TRACE "[call] ${FUNCNAME[0]}($*)" >&2       # Log that this function was called, with its arguments
	log_message INFO "Waiting ${1}s before trying again..." # Tell the operator how long the pause is
	sleep "$1" &                                            # Sleep in the background so a stop signal is handled immediately
	wait "$!" || true                                       # Wait for the sleep to finish (a signal interrupts this wait at once)
}                                                        # End of wait_before_retry

function next_retry_delay() {                         # Print the next wait: the current one doubled, but never above the cap
	local delay=$(($1 * 2))                              # Double the current wait
	if ((delay > NETWORK_RETRY_MAX_DELAY_SECONDS)); then # The doubled wait is above the cap
		delay=$NETWORK_RETRY_MAX_DELAY_SECONDS              # Use the cap instead
	fi                                                   # End of the cap check
	printf '%s' "$delay"                                 # Print the result for the caller to capture
}                                                     # End of next_retry_delay

function retry_network_command() {                                         # Run one network command again and again until it succeeds
	log_message TRACE "[call] ${FUNCNAME[0]}($*)" >&2                         # Log that this function was called, with its arguments
	local source_label="$1"                                                   # Label shown in front of the command's output lines
	local timeout_seconds="$2"                                                # Time limit for each single attempt
	shift 2                                                                   # Everything after the first two arguments is the command itself
	local attempt=0                                                           # How many attempts have been made so far
	local delay=$NETWORK_RETRY_DELAY_SECONDS                                  # The wait before the next attempt (starts at the first-retry wait)
	while true; do                                                            # Keep trying
		attempt=$((attempt + 1))                                                 # Count this attempt
		if run_logged_command "$source_label" "$timeout_seconds" "$@"; then      # Try the command
			return 0                                                                # It worked, so stop retrying
		fi                                                                       # End of the attempt
		log_message WARN "[${source_label}] attempt ${attempt} failed."          # Say that this attempt failed
		if ((NETWORK_MAX_ATTEMPTS > 0 && attempt >= NETWORK_MAX_ATTEMPTS)); then # An attempt limit is set and has been reached
			return 1                                                                # Give up and let the caller decide what to do
		fi                                                                       # End of the attempt-limit check
		wait_before_retry "$delay"                                               # Wait before the next attempt
		delay=$(next_retry_delay "$delay")                                       # Wait longer next time
	done                                                                      # End of the retry loop
}                                                                          # End of retry_network_command

function start_following_extra_logs() {                                                                           # Merge other apps' log files into this console output
	log_message TRACE "[call] ${FUNCNAME[0]}($*)" >&2                                                                # Log that this function was called, with its arguments
	local followed_log_file                                                                                          # Loop variable for each extra log file
	if [[ -z "$EXTRA_LOG_FILES" ]]; then                                                                             # Nothing configured
		log_message DEBUG "No extra log files configured (set EXTRA_LOG_FILES to merge other apps' logs)."              # Explain how to enable the feature
		return 0                                                                                                        # Nothing to follow
	fi                                                                                                               # End of the empty check
	if ! command -v tail >/dev/null; then                                                                            # tail -F is needed to follow files
		fail_and_exit "EXTRA_LOG_FILES is set but 'tail' is not installed."                                             # Stop because we cannot follow files
	fi                                                                                                               # End of the tail check
	for followed_log_file in $EXTRA_LOG_FILES; do                                                                    # Word splitting is intended: the list is space-separated
		log_message INFO "Following other app's log: ${followed_log_file}"                                              # Log which file is merged in
		{ tail -n 0 -F --pid=$$ -- "$followed_log_file" 2>&1 | relay_output_to_log INFO "${followed_log_file##*/}"; } & # Follow new lines until this script exits, labelled with the file name
		FOLLOWER_PIDS+=("$!")                                                                                           # Remember the background job so cleanup can stop it
	done                                                                                                             # End of the file loop
}                                                                                                                 # End of start_following_extra_logs

# ---------------- Statistics ----------------

function log_statistics() {                                                                                                                                                                                           # Log the running totals
	log_message TRACE "[call] ${FUNCNAME[0]}($*)" >&2                                                                                                                                                                    # Log that this function was called, with its arguments
	local saved_bytes=$((TOTAL_BYTES_BEFORE - TOTAL_BYTES_AFTER))                                                                                                                                                        # Bytes saved by compression so far
	local saved_percent=0                                                                                                                                                                                                # Percentage saved (stays 0 until something was compressed)
	local uptime_seconds=0                                                                                                                                                                                               # Seconds since the main loop started
	if [[ $TOTAL_BYTES_BEFORE -gt 0 ]]; then                                                                                                                                                                             # Avoid dividing by zero
		saved_percent=$((saved_bytes * 100 / TOTAL_BYTES_BEFORE))                                                                                                                                                           # Calculate the saving
	fi                                                                                                                                                                                                                   # End of the zero check
	if [[ $STARTED_EPOCH -ne 0 ]]; then                                                                                                                                                                                  # The loop has started
		uptime_seconds=$(($(date +%s) - STARTED_EPOCH))                                                                                                                                                                     # Calculate uptime
	fi                                                                                                                                                                                                                   # End of the uptime check
	log_message INFO "STATS: uptime=$(format_duration "$uptime_seconds") cycles=${CYCLE_NUMBER} compressed=${TOTAL_PDFS_COMPRESSED} kept-original=${TOTAL_PDFS_KEPT_ORIGINAL} oversized-ignored=${TOTAL_PDFS_OVERSIZED}" # Compression counters
	log_message INFO "STATS: PDF size before=$(format_bytes "$TOTAL_BYTES_BEFORE") after=$(format_bytes "$TOTAL_BYTES_AFTER") saved=$(format_bytes "$saved_bytes") (${saved_percent}%)"                                  # Size counters
	log_message INFO "STATS: pushes=${TOTAL_PUSHES} files-pushed=${TOTAL_FILES_PUSHED} data-pushed=$(format_bytes "$TOTAL_BYTES_PUSHED")"                                                                                # Push counters
}                                                                                                                                                                                                                     # End of log_statistics

function log_heartbeat_if_due() {                                                                                                                                                                                                                                                                                                                                                                     # Log a periodic status line so operators know the script is alive
	log_message TRACE "[call] ${FUNCNAME[0]}($*)" >&2                                                                                                                                                                                                                                                                                                                                                    # Log that this function was called, with its arguments
	local current_epoch                                                                                                                                                                                                                                                                                                                                                                                  # The current time
	current_epoch=$(date +%s)                                                                                                                                                                                                                                                                                                                                                                            # Read the current time
	if [[ $((current_epoch - LAST_HEARTBEAT_EPOCH)) -lt $HEARTBEAT_SECONDS ]]; then                                                                                                                                                                                                                                                                                                                      # Not due yet
		return 0                                                                                                                                                                                                                                                                                                                                                                                            # Nothing to do
	fi                                                                                                                                                                                                                                                                                                                                                                                                   # End of the due check
	LAST_HEARTBEAT_EPOCH=$current_epoch                                                                                                                                                                                                                                                                                                                                                                  # Remember when this heartbeat happened
	log_message INFO "HEARTBEAT: alive. progress to the three push limits: files=${STAGED_FILE_COUNT}/${MAX_BATCH_SIZE}, bytes=${STAGED_BYTES}/${MAX_BATCH_BYTES} ($(format_bytes "$STAGED_BYTES") of $(format_bytes "$MAX_BATCH_BYTES")), time=${SECONDS_SINCE_LAST_PUSH}s/${MAX_SECONDS_BETWEEN_PUSHES}s, free disk repo=$(available_megabytes .) MB temp=$(available_megabytes "$WORK_DIRECTORY") MB" # Status line
	log_statistics                                                                                                                                                                                                                                                                                                                                                                                       # Include the running totals
}                                                                                                                                                                                                                                                                                                                                                                                                     # End of log_heartbeat_if_due

# ---------------- Error Handling and Cleanup ----------------

function fail_and_exit() {                   # Log a fatal message and stop the whole script
	log_message FATAL "$1 Exiting immediately." # Tell the operator what went wrong
	exit 1                                      # Exit with a failure code (the EXIT trap cleans up)
}                                            # End of fail_and_exit

function handle_unexpected_error() {               # Called by the ERR trap when any command fails unexpectedly
	log_message FATAL "Command failed at line $1: $2" # Report the line number and the failing command
}                                                  # End of handle_unexpected_error

function cleanup_on_exit() {                                         # Called by the EXIT trap on every exit path
	local exit_code=$?                                                  # Capture the exit code first, before any other command changes it
	if [[ $OPTIONS_APPLIED -eq 0 ]]; then                               # A bad option or argument: nothing was started, so there is nothing to clean up
		return 0                                                           # Leave quietly (the exit code is not changed)
	fi                                                                  # End of the early-exit check
	local follower_pid                                                  # Loop variable for background log followers
	log_message INFO "Shutting down with exit code ${exit_code}."       # Log why and how we stop
	if [[ $STARTED_EPOCH -ne 0 ]]; then                                 # Only report statistics if the main loop ran
		log_statistics                                                     # Log the final totals
	fi                                                                  # End of the statistics check
	for follower_pid in "${FOLLOWER_PIDS[@]}"; do                       # Stop every background log follower
		kill "$follower_pid" 2>/dev/null || true                           # Ignore errors if it already ended
	done                                                                # End of the follower loop
	if [[ -n "${WORK_DIRECTORY}" && -d "${WORK_DIRECTORY}" ]]; then     # Only remove the directory if it was created
		log_message DEBUG "Removing temporary directory ${WORK_DIRECTORY}" # Log the cleanup
		rm -rf -- "${WORK_DIRECTORY}"                                      # Delete the temporary directory and its contents
	fi                                                                  # End of the directory check
}                                                                    # End of cleanup_on_exit

trap 'handle_unexpected_error "$LINENO" "$BASH_COMMAND"' ERR       # Log the failing line and command on any unexpected error
trap 'log_message WARN "Stop signal received."; exit 130' INT TERM # Exit cleanly when stopped with Ctrl-C or systemctl stop
trap cleanup_on_exit EXIT                                          # Always clean up and log the summary when the script ends

# ---------------- Startup Checks ----------------

function verify_dependencies() {                                                                                                             # Make sure every required program exists
	log_message TRACE "[call] ${FUNCNAME[0]}($*)" >&2                                                                                           # Log that this function was called, with its arguments
	log_message DEBUG "Checking system dependencies..."                                                                                         # Log the start of the check
	if ((BASH_VERSINFO[0] < 4 || (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] < 4))); then                                                        # mapfile -d requires Bash 4.4 or newer
		fail_and_exit "Bash 4.4 or newer is required (found ${BASH_VERSION})."                                                                     # Stop if Bash is too old
	fi                                                                                                                                          # End of the Bash version check
	local command_name                                                                                                                          # Loop variable for the command being checked
	local command_path                                                                                                                          # Holds the path where the command was found
	for command_name in gs git stat sort head cut wc grep sed awk tr df id uname date sleep timeout mktemp mkdir cp mv rm chmod xargs flock; do # Every external command the script uses
		if ! command_path=$(command -v "$command_name"); then                                                                                      # Look the command up in PATH
			fail_and_exit "Dependency '${command_name}' is not installed or not in PATH."                                                             # Stop if it is missing
		fi                                                                                                                                         # End of the lookup check
		log_message TRACE "Found '${command_name}' at ${command_path}"                                                                             # Log where it was found
	done                                                                                                                                        # End of the dependency loop
	if command -v fuser >/dev/null; then                                                                                                        # fuser is optional: it detects PDFs that another process still has open
		log_message DEBUG "'fuser' found: PDFs that another process still has open are skipped."                                                   # Log the extra protection
	else                                                                                                                                        # fuser is not installed
		log_message DEBUG "'fuser' not found (package psmisc): relying on .part files and the ${PDF_SETTLE_SECONDS}s settle time only."            # Explain what protects us instead
	fi                                                                                                                                          # End of the fuser check
	log_message DEBUG "All system dependencies verified."                                                                                       # Log success
}                                                                                                                                            # End of verify_dependencies

function acquire_single_instance_lock() {                                       # Refuse to run if another copy is already running
	log_message TRACE "[call] ${FUNCNAME[0]}($*)" >&2                              # Log that this function was called, with its arguments
	exec 9>"${LOCK_FILE}"                                                          # Open the lock file on file descriptor 9 (kept open for the script's lifetime)
	if ! flock --nonblock 9; then                                                  # Try to take the lock without waiting
		fail_and_exit "Another instance is already running (lock file ${LOCK_FILE})." # Stop if the lock is held
	fi                                                                             # End of the lock check
	log_message DEBUG "Acquired lock ${LOCK_FILE}"                                 # Log that we own the lock
}                                                                               # End of acquire_single_instance_lock

function enter_repository_root() {                                          # Change into the top directory of the Git repository
	log_message TRACE "[call] ${FUNCNAME[0]}($*)" >&2                          # Log that this function was called, with its arguments
	local repository_root                                                      # Will hold the repository's top-level path
	cd -- "${REPOSITORY_DIRECTORY}"                                            # Move into the configured directory (fails loudly if missing)
	if ! repository_root=$(git rev-parse --show-toplevel); then                # Ask Git where the repository root is
		fail_and_exit "'${REPOSITORY_DIRECTORY}' is not inside a Git repository." # Stop if this is not a Git repository
	fi                                                                         # End of the repository check
	cd -- "${repository_root}"                                                 # Work from the root so all paths are root-relative
	log_message INFO "Watching repository: ${repository_root}"                 # Log the repository being watched
}                                                                           # End of enter_repository_root

function verify_remote_access() {                                                                 # Check up front that pushing can work, before compressing anything
	log_message TRACE "[call] ${FUNCNAME[0]}($*)" >&2                                                # Log that this function was called, with its arguments
	local upstream_name                                                                              # Upstream branch, e.g. origin/main
	local remote_name                                                                                # Remote name, e.g. origin
	local remote_url                                                                                 # Remote URL with any credentials hidden
	if ! upstream_name=$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null); then   # Ask Git for the upstream branch
		fail_and_exit "The current branch has no upstream. Run: git push -u origin <branch>"            # Pull and push both need an upstream
	fi                                                                                               # End of the upstream check
	remote_name="${upstream_name%%/*}"                                                               # The remote is the part before the first slash
	remote_url=$(git remote get-url "$remote_name" | sed -e 's#//[^/@]*@#//***@#')                   # Read the URL and hide user:token@ credentials
	log_message INFO "Upstream branch: ${upstream_name}; remote '${remote_name}' URL: ${remote_url}" # Log where we push to
	if ! retry_network_command "git-ls-remote" 120 git ls-remote --heads "$remote_name"; then        # Prove that the network and credentials work (retried when the network is down)
		fail_and_exit "Cannot reach remote '${remote_name}' (network or credentials problem)."          # Stop only when the attempt limit is reached
	fi                                                                                               # End of the remote check
}                                                                                                 # End of verify_remote_access

function prepare_work_directory() {                                # Create a private temporary directory for Ghostscript
	log_message TRACE "[call] ${FUNCNAME[0]}($*)" >&2                 # Log that this function was called, with its arguments
	WORK_DIRECTORY=$(mktemp -d "${TMPDIR:-/tmp}/auto-sync.XXXXXX")    # Make a unique, writable directory (honours TMPDIR, defaults to /tmp)
	log_message DEBUG "Created temporary directory ${WORK_DIRECTORY}" # Log its location
}                                                                  # End of prepare_work_directory

function verify_free_disk_space() {                                                                                                               # Exit if the disk is nearly full, so commits and files never get corrupted
	log_message TRACE "[call] ${FUNCNAME[0]}($*)" >&2                                                                                                # Log that this function was called, with its arguments
	local repository_free_megabytes                                                                                                                  # Free space where the repository lives
	local work_free_megabytes                                                                                                                        # Free space where temporary files live
	repository_free_megabytes=$(available_megabytes .)                                                                                               # Measure the repository filesystem
	work_free_megabytes=$(available_megabytes "$WORK_DIRECTORY")                                                                                     # Measure the temporary filesystem
	log_message TRACE "Free disk space: repository=${repository_free_megabytes} MB, temp=${work_free_megabytes} MB (minimum ${MIN_FREE_DISK_MB} MB)" # Log the numbers
	if [[ $repository_free_megabytes -lt $MIN_FREE_DISK_MB ]]; then                                                                                  # Repository disk is too full
		fail_and_exit "Only ${repository_free_megabytes} MB free where the repository lives (minimum ${MIN_FREE_DISK_MB} MB)."                          # Stop before the disk fills up
	fi                                                                                                                                               # End of the repository disk check
	if [[ $work_free_megabytes -lt $MIN_FREE_DISK_MB ]]; then                                                                                        # Temporary disk is too full
		fail_and_exit "Only ${work_free_megabytes} MB free in the temp directory (minimum ${MIN_FREE_DISK_MB} MB)."                                     # Stop before the disk fills up
	fi                                                                                                                                               # End of the temporary disk check
}                                                                                                                                                 # End of verify_free_disk_space

function log_environment_report() {                                                                                                                                                                                                                                               # Log everything an operator needs to know about this run
	log_message TRACE "[call] ${FUNCNAME[0]}($*)" >&2                                                                                                                                                                                                                                # Log that this function was called, with its arguments
	local pending_change_count                                                                                                                                                                                                                                                       # Files Git sees as changed at startup
	pending_change_count=$(git status --porcelain --untracked-files=all | wc -l)                                                                                                                                                                                                     # Count changes (staged, unstaged and untracked)
	log_message INFO "===== STARTUP REPORT ====="                                                                                                                                                                                                                                    # Start of the report
	log_message INFO "Script version ${SCRIPT_VERSION}, PID $$, user $(id -un), host ${HOSTNAME:-unknown}"                                                                                                                                                                           # Who is running what, where
	log_message INFO "System: $(uname -sr), bash ${BASH_VERSION}"                                                                                                                                                                                                                    # Operating system and shell
	log_message INFO "Tools: $(git --version), ghostscript $(gs --version)"                                                                                                                                                                                                          # Versions of the programs we depend on
	log_message INFO "Git: branch $(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo none), HEAD $(git rev-parse --short HEAD 2>/dev/null || echo none)"                                                                                                                          # Where the repository stands
	log_message INFO "Changes pending at startup: ${pending_change_count} file(s)"                                                                                                                                                                                                   # How much work is waiting
	log_message INFO "Free disk: repository=$(available_megabytes .) MB, temp=$(available_megabytes "$WORK_DIRECTORY") MB"                                                                                                                                                           # Disk headroom
	log_message INFO "PUSH RULE: push as soon as the FIRST of these happens: ${MAX_BATCH_SIZE} files staged, ${MAX_BATCH_BYTES} bytes staged ($(format_bytes "$MAX_BATCH_BYTES")), or ${MAX_SECONDS_BETWEEN_PUSHES}s since the last push; checking every ${CHECK_INTERVAL_SECONDS}s" # Push settings
	log_message INFO "Ghostscript: quality ${GHOSTSCRIPT_QUALITY}, timeout ${GHOSTSCRIPT_TIMEOUT_SECONDS}s; GitHub file limit ${GITHUB_MAX_FILE_BYTES} bytes"                                                                                                                        # Compression settings
	log_message INFO "Downloads: PDFs are compressed only when no .part file exists and they were unchanged for ${PDF_SETTLE_SECONDS}s"                                                                                                                                              # Download-safety settings
	log_message INFO "Timeouts: local git ${GIT_LOCAL_TIMEOUT_SECONDS}s, network git ${GIT_NETWORK_TIMEOUT_SECONDS}s; minimum free disk ${MIN_FREE_DISK_MB} MB"                                                                                                                      # Safety settings
	log_message INFO "Network retries: first wait ${NETWORK_RETRY_DELAY_SECONDS}s (doubling, capped at ${NETWORK_RETRY_MAX_DELAY_SECONDS}s), maximum attempts ${NETWORK_MAX_ATTEMPTS} (0 = retry forever)"                                                                           # Retry settings
	log_message INFO "Logging: console only, level ${LOG_LEVEL_NUMBER}, heartbeat ${HEARTBEAT_SECONDS}s, extra logs '${EXTRA_LOG_FILES}', every skipped PDF logged ${LOG_EVERY_SKIPPED_PDF}, shell trace ${SHELL_TRACE:-0}"                                                          # Logging settings
	log_message INFO "===== END OF STARTUP REPORT ====="                                                                                                                                                                                                                             # End of the report
}                                                                                                                                                                                                                                                                                 # End of log_environment_report

# ---------------- Git Helpers ----------------

function count_staged_files() {                     # Print how many files are currently staged
	log_message TRACE "[call] ${FUNCNAME[0]}($*)" >&2  # Log that this function was called, with its arguments
	git diff --cached --name-only --no-renames | wc -l # Count staged paths (renames counted as two, matching the commit)
}                                                   # End of count_staged_files

function count_staged_bytes() {                                                                                                             # Print the total size in bytes of the staged files
	log_message TRACE "[call] ${FUNCNAME[0]}($*)" >&2                                                                                          # Log that this function was called, with its arguments
	git diff --cached --name-only -z --no-renames --diff-filter=AM | xargs -0 -r stat -c %s -- | awk '{ total += $1 } END { print total + 0 }' # Sum the sizes of staged files (deletions have no size)
}                                                                                                                                           # End of count_staged_bytes

function add_local_ignore_rule() {                                                    # Add a rule to the repository's local ignore file (never committed)
	log_message TRACE "[call] ${FUNCNAME[0]}($*)" >&2                                    # Log that this function was called, with its arguments
	local ignore_rule="$1"                                                               # The gitignore rule to add
	local exclude_file                                                                   # Path to the repository's local ignore file
	exclude_file=$(git rev-parse --git-path info/exclude)                                # Ask Git where the local exclude file lives
	mkdir -p -- "${exclude_file%/*}"                                                     # Make sure its directory exists
	if [[ -f "$exclude_file" ]] && grep -q -x -F -- "$ignore_rule" "$exclude_file"; then # Check whether the rule is already there
		log_message DEBUG "Local ignore rule already present: ${ignore_rule}"               # Log that nothing needs to change
		return 0                                                                            # Nothing to add
	fi                                                                                   # End of the duplicate check
	printf '%s\n' "$ignore_rule" >>"$exclude_file"                                       # Append the rule on its own line
	log_message INFO "Added local ignore rule '${ignore_rule}' to ${exclude_file}"       # Log the change
}                                                                                     # End of add_local_ignore_rule

function pdf_is_finished_downloading() {                                                                                          # Succeed only when a PDF is completely downloaded and nothing is still writing to it (logs go to stderr because this runs inside pipelines)
	local pdf_path="$1"                                                                                                              # The PDF to check
	local modified_epoch                                                                                                             # When the PDF was last written
	local age_seconds                                                                                                                # Seconds since the PDF was last written
	if [[ -e "${pdf_path}.part" ]]; then                                                                                             # The downloader writes '<name>.pdf.part' and renames it to '<name>.pdf' only when finished
		log_message TRACE "Skipping '${pdf_path}': a .part file exists, so it is still downloading." >&2                                # Say why it is skipped
		return 1                                                                                                                        # Not finished
	fi                                                                                                                               # End of the .part check
	if ! modified_epoch=$(stat -c %Y -- "$pdf_path" 2>/dev/null); then                                                               # Read the last-modified time (fails if the file vanished)
		log_message TRACE "Skipping '${pdf_path}': it no longer exists." >&2                                                            # Say why it is skipped
		return 1                                                                                                                        # Not usable
	fi                                                                                                                               # End of the modification-time read
	printf -v age_seconds '%(%s)T' -1                                                                                                # Read the current time without starting another process
	age_seconds=$((age_seconds - modified_epoch))                                                                                    # Work out how long ago it was last written
	if [[ $age_seconds -lt $PDF_SETTLE_SECONDS ]]; then                                                                              # Written very recently: a download may still be finishing or about to be replaced
		log_message TRACE "Skipping '${pdf_path}': modified ${age_seconds}s ago (it must be unchanged for ${PDF_SETTLE_SECONDS}s)." >&2 # Say why it is skipped
		return 1                                                                                                                        # Not settled yet
	fi                                                                                                                               # End of the settle check
	if command -v fuser >/dev/null && fuser -s -- "$pdf_path" 2>/dev/null; then                                                      # If fuser exists, check whether any process still has the file open
		log_message TRACE "Skipping '${pdf_path}': another process still has it open." >&2                                              # Say why it is skipped
		return 1                                                                                                                        # Still in use
	fi                                                                                                                               # End of the open-file check
	if [[ "$(head -c 5 -- "$pdf_path" 2>/dev/null || true)" != "%PDF-" ]]; then                                                      # A real PDF starts with the %PDF- signature (this also rejects empty files)
		log_message TRACE "Skipping '${pdf_path}': it does not start with the PDF signature yet." >&2                                   # Say why it is skipped
		return 1                                                                                                                        # Not a complete PDF
	fi                                                                                                                               # End of the signature check
	return 0                                                                                                                         # Every check passed: the PDF is finished
}                                                                                                                                 # End of pdf_is_finished_downloading

function filter_finished_pdfs() {                                                                  # Read NUL-separated PDF paths on standard input and print only the finished ones (slow per file, so only used on a short list)
	log_message TRACE "[call] ${FUNCNAME[0]}($*)" >&2                                                 # Log that this function was called, with its arguments
	local pdf_path                                                                                    # Holds one path
	while IFS= read -r -d '' pdf_path; do                                                             # Read every NUL-terminated path
		if pdf_is_finished_downloading "$pdf_path"; then                                                 # Keep it only if it is completely downloaded
			log_message TRACE "[scan] accepted '${pdf_path}': finished downloading, ready to compress." >&2 # Say that it passed every check
			printf '%s\0' "$pdf_path"                                                                       # Pass it on, NUL-terminated
		fi                                                                                               # End of the finished check
	done                                                                                              # End of the path loop
}                                                                                                  # End of filter_finished_pdfs

function log_pipeline_count() {                            # Pass NUL-separated records through unchanged and log how many went by (shows every scan stage)
	log_message TRACE "[call] ${FUNCNAME[0]}($*)" >&2         # Log that this function was called, with its arguments
	local stage_label="$1"                                    # What was counted
	local record                                              # Holds one record
	local counted=0                                           # How many records passed
	while IFS= read -r -d '' record; do                       # Read every NUL-terminated record
		counted=$((counted + 1))                                 # Count it
		printf '%s\0' "$record"                                  # Pass it on unchanged
	done                                                      # End of the record loop
	log_message TRACE "[scan] ${stage_label}: ${counted}" >&2 # Report the total (stderr, because this runs inside a pipeline)
}                                                          # End of log_pipeline_count

function filter_settled_pdfs() {                                                                                                                                             # Fast bulk filter: read NUL-separated "mtime<TAB>size<TAB>path" records and keep those unchanged for PDF_SETTLE_SECONDS (uses no external programs, so it is fast even for 10000+ files)
	log_message TRACE "[call] ${FUNCNAME[0]}($*)" >&2                                                                                                                           # Log that this function was called, with its arguments
	local record                                                                                                                                                                # Holds one record
	local tab=$'\t'                                                                                                                                                             # A tab character, used to split the record
	local modified_epoch                                                                                                                                                        # The modification time part of the record
	local size_and_path                                                                                                                                                         # The "size<TAB>path" part of the record
	local current_epoch                                                                                                                                                         # The current time, read once
	local total_count=0                                                                                                                                                         # How many PDFs were checked
	local kept_count=0                                                                                                                                                          # How many PDFs were old enough
	local too_recent_count=0                                                                                                                                                    # How many PDFs were skipped because they were written moments ago
	printf -v current_epoch '%(%s)T' -1                                                                                                                                         # Read the current time without starting another process
	while IFS= read -r -d '' record; do                                                                                                                                         # Read every NUL-terminated record
		total_count=$((total_count + 1))                                                                                                                                           # Count this PDF
		if ((total_count % 2000 == 0)); then                                                                                                                                       # Show progress now and then on huge repositories
			log_message TRACE "[scan] settle check progress: ${total_count} PDFs checked so far..." >&2                                                                               # Say that the script is still working
		fi                                                                                                                                                                         # End of the progress report
		modified_epoch="${record%%"$tab"*}"                                                                                                                                        # Everything before the first tab is the modification time
		size_and_path="${record#*"$tab"}"                                                                                                                                          # Everything after the first tab is "size<TAB>path"
		if ((current_epoch - modified_epoch >= PDF_SETTLE_SECONDS)); then                                                                                                          # Unchanged for long enough
			kept_count=$((kept_count + 1))                                                                                                                                            # Count it
			printf '%s\0' "$size_and_path"                                                                                                                                            # Pass it on for sorting by size
		else                                                                                                                                                                       # Written moments ago: a download may still be finishing
			too_recent_count=$((too_recent_count + 1))                                                                                                                                # Count it
			if [[ $LOG_EVERY_SKIPPED_PDF == "1" ]]; then                                                                                                                              # One line per skipped file was requested
				log_message TRACE "[scan] skipping '${size_and_path#*"$tab"}': modified $((current_epoch - modified_epoch))s ago (it must be unchanged for ${PDF_SETTLE_SECONDS}s)." >&2 # Say why it is skipped
			fi                                                                                                                                                                        # End of the per-file report
		fi                                                                                                                                                                         # End of the settle check
	done                                                                                                                                                                        # End of the record loop
	log_message TRACE "[scan] settle check: ${total_count} PDFs checked, ${kept_count} settled, ${too_recent_count} too recent." >&2                                            # Report the totals (stderr, because this runs inside a pipeline)
}                                                                                                                                                                            # End of filter_settled_pdfs

function list_pdfs_to_compress() {                                                      # Print (NUL-separated) the smallest finished, unstaged PDFs, up to a limit
	log_message TRACE "[call] ${FUNCNAME[0]}($*)" >&2                                      # Log that this function was called, with its arguments
	local maximum_files="$1"                                                               # How many PDFs to list at most
	log_message TRACE "[scan] asking Git for new and modified files..." >&2                # Say what is happening now
	{                                                                                      # Start a group that merges two Git listings
		git ls-files -z --others --exclude-standard                                           # New (untracked) files, NUL-separated so odd filenames are safe
		git diff --name-only -z --diff-filter=MT                                              # Modified files that still exist (deleted files are excluded on purpose)
	} | log_pipeline_count "files reported by Git (all types)" |                           # Show how many files Git found
		grep -z -i '\.pdf$' |                                                                 # Keep only PDF paths (case-insensitive); '*.pdf.part' files never match
		log_pipeline_count "PDF files among them" |                                           # Show how many are PDFs
		xargs -0 -r stat --printf='%Y\t%s\t%n\0' -- |                                         # One stat run for all files: modification time, size and path
		filter_settled_pdfs |                                                                 # Fast bulk filter: drop PDFs written within the last PDF_SETTLE_SECONDS
		sort -z -n |                                                                          # Sort by size, smallest first
		sed -z -n "1,${maximum_files}p" |                                                     # Keep only as many as fit in the batch (reads ALL input, so sort never gets a broken pipe)
		cut -z -f2- |                                                                         # Drop the size column, leaving only the paths
		log_pipeline_count "smallest PDFs selected for the checks (limit ${maximum_files})" | # Show how many were selected
		filter_finished_pdfs |                                                                # Slow per-file checks (.part file, open file, PDF signature) on this SHORT list only
		log_pipeline_count "finished PDFs ready to compress"                                  # Show how many passed every check
}                                                                                       # End of list_pdfs_to_compress

function list_other_changed_files() {                                          # Print (NUL-separated) non-PDF changes plus deletions, up to a limit
	log_message TRACE "[call] ${FUNCNAME[0]}($*)" >&2                             # Log that this function was called, with its arguments
	local maximum_files="$1"                                                      # How many paths to list at most
	{                                                                             # Start a group that merges three Git listings
		git ls-files -z --others --exclude-standard | grep -z -v -i '\.pdf$' || true # New non-PDF files (grep exits 1 when nothing matches, which is fine)
		git diff --name-only -z --diff-filter=MT | grep -z -v -i '\.pdf$' || true    # Modified non-PDF files
		git diff --name-only -z --diff-filter=D                                      # Deleted files of any type
	} | sort -z -u | sed -z -n "1,${maximum_files}p"                              # Remove duplicates and keep only as many as fit (sed reads ALL input, so sort never gets a broken pipe)
}                                                                              # End of list_other_changed_files

# ---------------- Phase 1: PDF Compression ----------------

function exclude_oversized_pdf() {                                                         # Tell Git (locally) to ignore a PDF that GitHub would reject
	log_message TRACE "[call] ${FUNCNAME[0]}($*)" >&2                                         # Log that this function was called, with its arguments
	local pdf_path="$1"                                                                       # The oversized PDF
	local escaped_path                                                                        # PDF path with glob characters escaped
	escaped_path=$(printf '%s' "$pdf_path" | sed -e 's/[][\\*?]/\\&/g')                       # Escape characters that gitignore treats as wildcards
	add_local_ignore_rule "/${escaped_path}"                                                  # Add an anchored ignore rule (the file stays on disk)
	TOTAL_PDFS_OVERSIZED=$((TOTAL_PDFS_OVERSIZED + 1))                                        # Count the oversized file
	log_message WARN "'${pdf_path}' exceeds the GitHub size limit and is now ignored by Git." # Tell the operator what happened
}                                                                                          # End of exclude_oversized_pdf

function compress_and_stage_pdf() {                                                                                              # Compress one PDF with Ghostscript and stage it
	log_message TRACE "[call] ${FUNCNAME[0]}($*)" >&2                                                                               # Log that this function was called, with its arguments
	local pdf_path="$1"                                                                                                             # The PDF to process
	local position_text="$2"                                                                                                        # Progress marker such as [3/57]
	local temporary_input="${WORK_DIRECTORY}/input.pdf"                                                                             # Private copy of the PDF that Ghostscript reads
	local temporary_output="${WORK_DIRECTORY}/output.pdf"                                                                           # Where Ghostscript writes the result
	local original_size_bytes                                                                                                       # Size before compression
	local original_signature                                                                                                        # Last-modified time and size of the PDF when compression started, used to detect a re-download
	local compressed_size_bytes                                                                                                     # Size of Ghostscript's output
	local final_size_bytes                                                                                                          # Size of the file that will actually be committed
	local saved_percent                                                                                                             # How much smaller the file became
	local started_epoch                                                                                                             # When this PDF started
	local elapsed_seconds                                                                                                           # How long this PDF took
	local ghostscript_options=(-sDEVICE=pdfwrite -dCompatibilityLevel=1.4 "-dPDFSETTINGS=${GHOSTSCRIPT_QUALITY}" -dNOPAUSE -dBATCH) # Core Ghostscript options
	if [[ $LOG_LEVEL_NUMBER -gt 1 ]]; then                                                                                          # Only be chatty when DEBUG or TRACE logging is on
		ghostscript_options+=(-dQUIET)                                                                                                 # Silence Ghostscript's per-page output
	fi                                                                                                                              # End of the verbosity check

	original_size_bytes=$(stat -c %s -- "$pdf_path")                                                       # Record the original size in bytes
	original_signature=$(stat -c '%Y %s' -- "$pdf_path")                                                   # Record the modification time and size, to notice if the PDF is replaced while we work
	started_epoch=$(date +%s)                                                                              # Record the start time
	log_message INFO "COMPRESSING ${position_text} '${pdf_path}' ($(format_bytes "$original_size_bytes"))" # Show which file is being compressed right now
	log_message TRACE "[file] copying '${pdf_path}' to '${temporary_input}'"                               # Log the copy into the temporary directory
	cp -- "$pdf_path" "$temporary_input"                                                                   # Copy the PDF into the temporary directory
	rm -f -- "$temporary_output"                                                                           # Remove any leftover output from a previous file

	if ! run_logged_command "ghostscript ${pdf_path}" "$GHOSTSCRIPT_TIMEOUT_SECONDS" gs "${ghostscript_options[@]}" -sOutputFile="$temporary_output" "$temporary_input"; then # Run Ghostscript; every output line is labelled with the PDF name
		fail_and_exit "Ghostscript failed or timed out for '${pdf_path}'."                                                                                                       # Stop on any compression failure
	fi                                                                                                                                                                        # End of the Ghostscript check
	if [[ ! -s "$temporary_output" ]]; then                                                                                                                                   # Ghostscript "succeeded" but wrote nothing
		fail_and_exit "Ghostscript produced an empty file for '${pdf_path}'."                                                                                                    # Stop to avoid data loss
	fi                                                                                                                                                                        # End of the empty-output check

	if [[ "$(stat -c '%Y %s' -- "$pdf_path" 2>/dev/null || true)" != "$original_signature" ]]; then                                                                           # The PDF changed while Ghostscript was running (for example the downloader saved a new copy)
		log_message WARN "'${pdf_path}' changed while it was being compressed (probably downloaded again); discarding the compressed copy, it will be handled in a later cycle." # Explain why nothing is replaced
		rm -f -- "$temporary_input" "$temporary_output"                                                                                                                          # Remove both temporary files
		return 0                                                                                                                                                                 # Never overwrite a file that was replaced in the meantime
	fi                                                                                                                                                                        # End of the changed-while-compressing check

	compressed_size_bytes=$(stat -c %s -- "$temporary_output")                                                                                                 # Measure Ghostscript's output
	if [[ $compressed_size_bytes -lt $original_size_bytes ]]; then                                                                                             # Only use the output if it is actually smaller
		log_message TRACE "[file] replacing '${pdf_path}' with the compressed copy"                                                                               # Log the replacement
		mv -- "$temporary_output" "$pdf_path"                                                                                                                     # Replace the original with the compressed version
		log_message TRACE "[file] setting permissions 644 on '${pdf_path}'"                                                                                       # Log the permission change
		chmod 644 -- "$pdf_path"                                                                                                                                  # mktemp files are 0600; restore normal permissions
		saved_percent=$(((original_size_bytes - compressed_size_bytes) * 100 / original_size_bytes))                                                              # Work out the saving (original is non-zero here)
		TOTAL_PDFS_COMPRESSED=$((TOTAL_PDFS_COMPRESSED + 1))                                                                                                      # Count the compressed PDF
		TOTAL_BYTES_BEFORE=$((TOTAL_BYTES_BEFORE + original_size_bytes))                                                                                          # Add to the "before" total
		TOTAL_BYTES_AFTER=$((TOTAL_BYTES_AFTER + compressed_size_bytes))                                                                                          # Add to the "after" total
		log_message INFO "COMPRESSED '${pdf_path}': $(format_bytes "$original_size_bytes") -> $(format_bytes "$compressed_size_bytes") (saved ${saved_percent}%)" # Log before and after sizes
	else                                                                                                                                                       # Compression did not help
		TOTAL_PDFS_KEPT_ORIGINAL=$((TOTAL_PDFS_KEPT_ORIGINAL + 1))                                                                                                # Count the unchanged PDF
		TOTAL_BYTES_BEFORE=$((TOTAL_BYTES_BEFORE + original_size_bytes))                                                                                          # Add the original to the "before" total
		TOTAL_BYTES_AFTER=$((TOTAL_BYTES_AFTER + original_size_bytes))                                                                                            # The file is unchanged, so "after" equals "before"
		log_message INFO "KEPT ORIGINAL '${pdf_path}': compressed copy was not smaller (${compressed_size_bytes} >= ${original_size_bytes} bytes)"                # Log why nothing changed
	fi                                                                                                                                                         # End of the size comparison
	rm -f -- "$temporary_input" "$temporary_output"                                                                                                            # Remove both temporary files
	elapsed_seconds=$(($(date +%s) - started_epoch))                                                                                                           # Measure how long this PDF took
	log_message DEBUG "Finished '${pdf_path}' in ${elapsed_seconds}s"                                                                                          # Log the duration

	final_size_bytes=$(stat -c %s -- "$pdf_path")               # Measure the file that would be committed
	if [[ $final_size_bytes -gt $GITHUB_MAX_FILE_BYTES ]]; then # GitHub would reject this file and break every push
		exclude_oversized_pdf "$pdf_path"                          # Ignore it locally instead of staging it
		return 0                                                   # Move on to the next PDF
	fi                                                          # End of the oversize check

	if ! run_logged_command "git-add" "$GIT_LOCAL_TIMEOUT_SECONDS" git add --verbose -- "$pdf_path"; then                         # Stage the PDF so it counts as processed
		fail_and_exit "Failed to stage '${pdf_path}'."                                                                               # Stop to avoid recompressing the same file forever
	fi                                                                                                                            # End of the staging check
	STAGED_BYTES=$((STAGED_BYTES + final_size_bytes))                                                                             # Keep the running byte total up to date within this cycle
	log_message DEBUG "Staged '${pdf_path}'. Batch so far: $(format_bytes "$STAGED_BYTES") of $(format_bytes "$MAX_BATCH_BYTES")" # Show how full the batch is
}                                                                                                                              # End of compress_and_stage_pdf

function compress_pending_pdfs() {                                                                      # Compress and stage as many finished PDFs as fit in the batch
	log_message TRACE "[call] ${FUNCNAME[0]}($*)" >&2                                                      # Log that this function was called, with its arguments
	local staged_file_count                                                                                # Files already staged
	local room_in_batch                                                                                    # How many more files may be staged
	local pdf_path                                                                                         # Loop variable for each PDF
	local pdf_index=0                                                                                      # Position of the current PDF in this cycle
	local pdf_total                                                                                        # Number of PDFs selected in this cycle
	local pending_pdfs=()                                                                                  # Array holding the PDFs selected this cycle
	staged_file_count=$(count_staged_files)                                                                # Count what is already staged
	room_in_batch=$((MAX_BATCH_SIZE - staged_file_count))                                                  # Compute the remaining room in this batch
	if [[ $room_in_batch -le 0 ]]; then                                                                    # Batch is already full
		log_message DEBUG "Batch is full (${staged_file_count}/${MAX_BATCH_SIZE}); not compressing more yet." # Explain why nothing is compressed
		return 0                                                                                              # Nothing to do this cycle
	fi                                                                                                     # End of the room check

	log_message DEBUG "Scanning for finished, unstaged PDFs (this can take a while when thousands of files are pending)..."                # Tell the operator why nothing happens for a moment
	local scan_started_seconds=$SECONDS                                                                                                    # Remember when the scan started
	mapfile -d '' -t pending_pdfs < <(list_pdfs_to_compress "$room_in_batch" || true)                                                      # Load the smallest finished, unstaged PDFs (NUL-safe) into the array
	log_message DEBUG "Scan finished in $((SECONDS - scan_started_seconds))s: ${#pending_pdfs[@]} PDF(s) selected."                        # Report how long the scan took
	pdf_total=${#pending_pdfs[@]}                                                                                                          # Remember how many were selected
	if [[ $pdf_total -eq 0 ]]; then                                                                                                        # No finished PDFs waiting
		log_message TRACE "No finished, unstaged PDFs found this cycle."                                                                      # Log the empty result
		return 0                                                                                                                              # Nothing to do this cycle
	fi                                                                                                                                     # End of the empty check
	log_message INFO "Found ${pdf_total} finished, unstaged PDF(s) to compress (smallest first; room for ${room_in_batch} in this batch)." # Log how many were found
	verify_free_disk_space                                                                                                                 # Make sure there is disk space before doing heavy work
	STAGED_BYTES=$(count_staged_bytes)                                                                                                     # Start from the real number of staged bytes

	local queued_path                                                                                                               # Loop variable for the queue listing
	for queued_path in "${pending_pdfs[@]}"; do                                                                                     # Show the whole queue before compressing
		log_message TRACE "[queue] ${queued_path}"                                                                                     # One line per queued PDF
	done                                                                                                                            # End of the queue listing
	for pdf_path in "${pending_pdfs[@]}"; do                                                                                        # Process each PDF in order
		pdf_index=$((pdf_index + 1))                                                                                                   # Move the progress counter forward
		if [[ $STAGED_BYTES -ge $MAX_BATCH_BYTES ]]; then                                                                              # The batch is big enough for one push
			log_message INFO "Batch size limit reached (${STAGED_BYTES} >= ${MAX_BATCH_BYTES} bytes); the rest waits for the next batch." # Explain why we stop
			break                                                                                                                         # Stop compressing so the batch can be pushed
		fi                                                                                                                             # End of the batch-bytes check
		if [[ ! -f "$pdf_path" ]]; then                                                                                                # The file vanished after it was listed
			log_message DEBUG "'${pdf_path}' no longer exists; skipping."                                                                 # Log the skip
			continue                                                                                                                      # Go to the next PDF
		fi                                                                                                                             # End of the existence check
		if ! pdf_is_finished_downloading "$pdf_path"; then                                                                             # The list was built earlier; the downloader may have started a new download of this PDF since
			log_message INFO "'${pdf_path}' is being downloaded again or changed since it was listed; skipping it for now."               # Explain the skip
			continue                                                                                                                      # Go to the next PDF; it is picked up again once it has settled
		fi                                                                                                                             # End of the finished-downloading re-check
		compress_and_stage_pdf "$pdf_path" "[${pdf_index}/${pdf_total}]"                                                               # Compress the PDF and stage it
	done                                                                                                                            # End of the PDF loop
}                                                                                                                                # End of compress_pending_pdfs

# ---------------- Phase 2: Batch Push ----------------

function refresh_batch_counters() {                                                                                                                                                                    # Update the counters used by the push decision
	log_message TRACE "[call] ${FUNCNAME[0]}($*)" >&2                                                                                                                                                     # Log that this function was called, with its arguments
	STAGED_FILE_COUNT=$(count_staged_files)                                                                                                                                                               # Number of files staged right now
	STAGED_BYTES=$(count_staged_bytes)                                                                                                                                                                    # Total bytes staged right now
	SECONDS_SINCE_LAST_PUSH=$(($(date +%s) - LAST_PUSH_EPOCH))                                                                                                                                            # Seconds since the last push
	log_message TRACE "Status: staged ${STAGED_FILE_COUNT}/${MAX_BATCH_SIZE} files, ${STAGED_BYTES}/${MAX_BATCH_BYTES} bytes, ${SECONDS_SINCE_LAST_PUSH}s/${MAX_SECONDS_BETWEEN_PUSHES}s since last push" # Log the counters
}                                                                                                                                                                                                      # End of refresh_batch_counters

function batch_is_ready() {                                                                                            # Succeeds (returns 0) when a push should happen now
	log_message TRACE "[call] ${FUNCNAME[0]}($*)" >&2                                                                     # Log that this function was called, with its arguments
	if [[ $STAGED_FILE_COUNT -ge $MAX_BATCH_SIZE ]]; then                                                                 # A full batch is staged
		log_message INFO "Push trigger: ${STAGED_FILE_COUNT} files staged (limit ${MAX_BATCH_SIZE})."                        # Log the reason
		return 0                                                                                                             # Ready to push
	fi                                                                                                                    # End of the batch-size check
	if [[ $STAGED_BYTES -ge $MAX_BATCH_BYTES ]]; then                                                                     # The staged files are big enough for one push
		log_message INFO "Push trigger: ${STAGED_BYTES} bytes staged (limit ${MAX_BATCH_BYTES})."                            # Log the reason
		return 0                                                                                                             # Ready to push
	fi                                                                                                                    # End of the batch-bytes check
	if [[ $SECONDS_SINCE_LAST_PUSH -ge $MAX_SECONDS_BETWEEN_PUSHES ]]; then                                               # The maximum wait has passed
		log_message INFO "Push trigger: ${SECONDS_SINCE_LAST_PUSH}s since last push (limit ${MAX_SECONDS_BETWEEN_PUSHES}s)." # Log the reason
		return 0                                                                                                             # Ready to push
	fi                                                                                                                    # End of the timer check
	return 1                                                                                                              # Not ready yet
}                                                                                                                      # End of batch_is_ready

function stage_other_changed_files() {                 # Fill leftover batch room with non-PDF changes and deletions
	log_message TRACE "[call] ${FUNCNAME[0]}($*)" >&2     # Log that this function was called, with its arguments
	local staged_file_count                               # Files already staged
	local room_in_batch                                   # How many more files may be staged
	local other_files=()                                  # Array holding the extra paths
	staged_file_count=$(count_staged_files)               # Count what is already staged
	room_in_batch=$((MAX_BATCH_SIZE - staged_file_count)) # Compute the remaining room
	if [[ $room_in_batch -le 0 ]]; then                   # No room left
		return 0                                             # Nothing to add
	fi                                                    # End of the room check

	mapfile -d '' -t other_files < <(list_other_changed_files "$room_in_batch" || true) # Load the extra paths (NUL-safe) into the array
	if [[ ${#other_files[@]} -eq 0 ]]; then                                             # Nothing extra to stage
		log_message DEBUG "No additional non-PDF changes to stage."                        # Log the empty result
		return 0                                                                           # Nothing to add
	fi                                                                                  # End of the empty check

	log_message INFO "Staging ${#other_files[@]} additional non-PDF change(s)..."                                                          # Log how many are being added
	if ! printf '%s\0' "${other_files[@]}" | run_logged_command "git-add" "$GIT_LOCAL_TIMEOUT_SECONDS" xargs -0 git add --verbose --; then # Stage them all, safely handling odd filenames
		fail_and_exit "Failed to stage additional files."                                                                                     # Stop on staging failure
	fi                                                                                                                                     # End of the staging check
}                                                                                                                                       # End of stage_other_changed_files

function commit_staged_batch() {                                                                         # Commit everything that is staged
	log_message TRACE "[call] ${FUNCNAME[0]}($*)" >&2                                                       # Log that this function was called, with its arguments
	local staged_file_count                                                                                 # Number of files in this commit
	local commit_timestamp                                                                                  # UTC time for the commit message
	local commit_message                                                                                    # Full commit message
	staged_file_count=$(count_staged_files)                                                                 # Count the staged files
	commit_timestamp=$(date -u +'%Y-%m-%d %H:%M:%S UTC')                                                    # Build a UTC timestamp
	commit_message="Auto-sync commit (${staged_file_count} files) - ${commit_timestamp}"                    # Build the commit message
	log_message INFO "Committing ${staged_file_count} file(s): '${commit_message}'"                         # Log what is being committed
	git diff --cached --name-status --no-renames | relay_output_to_log DEBUG "commit-file"                  # List every file in the commit with its status letter (A/M/D)
	if ! run_logged_command "git-commit" "$GIT_LOCAL_TIMEOUT_SECONDS" git commit -m "$commit_message"; then # Create the commit
		fail_and_exit "Git commit failed."                                                                     # Stop on commit failure
	fi                                                                                                      # End of the commit check
	log_message INFO "Commit created: $(git rev-parse --short HEAD)"                                        # Log the new commit's short hash
}                                                                                                        # End of commit_staged_batch

function rebase_is_in_progress() {                                                                    # Succeeds when a rebase is stuck half-way (a conflict, which is not a network problem)
	[[ -d "$(git rev-parse --git-path rebase-merge)" || -d "$(git rev-parse --git-path rebase-apply)" ]] # Git keeps one of these directories while a rebase is unfinished
}                                                                                                     # End of rebase_is_in_progress

function pull_and_push_once() {                                                                                             # One attempt: pull (rebase), then push; returns non-zero on any failure
	log_message TRACE "[call] ${FUNCNAME[0]}($*)" >&2                                                                          # Log that this function was called, with its arguments
	log_message INFO "Pulling remote changes (rebase + autostash)..."                                                          # Log the start of the pull
	if ! run_logged_command "git-pull" "$GIT_NETWORK_TIMEOUT_SECONDS" git pull --verbose --progress --rebase --autostash; then # Pull; autostash protects unstaged work
		return 1                                                                                                                  # The caller decides what this failure means
	fi                                                                                                                         # End of the pull check
	log_message DEBUG "Pull completed (or already up to date)."                                                                # Log the pull success
	log_message INFO "Pushing to remote repository..."                                                                         # Log the start of the push
	run_logged_command "git-push" "$GIT_NETWORK_TIMEOUT_SECONDS" git push --verbose --progress                                 # Push (its exit code is this function's result)
}                                                                                                                           # End of pull_and_push_once

function sync_with_remote() {                                                                     # Pull and push, repeating the whole sequence after network failures
	log_message TRACE "[call] ${FUNCNAME[0]}($*)" >&2                                                # Log that this function was called, with its arguments
	local attempt=0                                                                                  # How many attempts have been made so far
	local delay=$NETWORK_RETRY_DELAY_SECONDS                                                         # The wait before the next attempt (starts at the first-retry wait)
	while true; do                                                                                   # Keep trying
		attempt=$((attempt + 1))                                                                        # Count this attempt
		if pull_and_push_once; then                                                                     # Try pull + push
			log_message DEBUG "Pull and push succeeded on attempt ${attempt}."                             # Log the success
			return 0                                                                                       # Done
		fi                                                                                              # End of the attempt
		if rebase_is_in_progress; then                                                                  # A rebase conflict: retrying cannot fix it
			git rebase --abort || true                                                                     # Leave the repository clean (harmless if nothing is in progress)
			fail_and_exit "Rebase conflict while pulling; a person must resolve it."                       # Stop, because a person has to decide
		fi                                                                                              # End of the conflict check
		log_message WARN "Pull/push failed (attempt ${attempt}); assuming a network or remote problem." # Say what we assume
		if ((NETWORK_MAX_ATTEMPTS > 0 && attempt >= NETWORK_MAX_ATTEMPTS)); then                        # An attempt limit is set and has been reached
			fail_and_exit "Pull/push still failing after ${attempt} attempts."                             # Stop and let systemd restart the script
		fi                                                                                              # End of the attempt-limit check
		wait_before_retry "$delay"                                                                      # Wait before the next attempt
		delay=$(next_retry_delay "$delay")                                                              # Wait longer next time
	done                                                                                             # End of the retry loop
}                                                                                                 # End of sync_with_remote

function push_batch() {                                                                                                                                                                               # Run the full stage, commit, pull, push sequence
	log_message TRACE "[call] ${FUNCNAME[0]}($*)" >&2                                                                                                                                                    # Log that this function was called, with its arguments
	local staged_file_count                                                                                                                                                                              # Files in the batch after filling it
	local batch_bytes                                                                                                                                                                                    # Bytes in the batch
	local started_epoch                                                                                                                                                                                  # When this push sequence started
	local elapsed_seconds                                                                                                                                                                                # How long the sequence took
	local bytes_per_second                                                                                                                                                                               # Average throughput
	stage_other_changed_files                                                                                                                                                                            # Top up the batch with non-PDF changes if there is room
	staged_file_count=$(count_staged_files)                                                                                                                                                              # Count the final batch
	if [[ $staged_file_count -eq 0 ]]; then                                                                                                                                                              # Timer fired but there is nothing to push
		log_message INFO "Nothing staged; resetting the push timer."                                                                                                                                        # Explain the reset
		LAST_PUSH_EPOCH=$(date +%s)                                                                                                                                                                         # Restart the timer so we do not retry every cycle
		return 0                                                                                                                                                                                            # Done for this cycle
	fi                                                                                                                                                                                                   # End of the empty-batch check
	batch_bytes=$(count_staged_bytes)                                                                                                                                                                    # Measure the batch before committing
	started_epoch=$(date +%s)                                                                                                                                                                            # Record the start time
	log_message INFO "PUSH STARTING: ${staged_file_count} file(s), $(format_bytes "$batch_bytes")"                                                                                                       # Announce the push
	commit_staged_batch                                                                                                                                                                                  # Commit the batch (once, so retries never create duplicate commits)
	sync_with_remote                                                                                                                                                                                     # Pull and push, retrying the whole sequence on network failures
	elapsed_seconds=$(($(date +%s) - started_epoch))                                                                                                                                                     # Measure the whole sequence
	bytes_per_second=$((batch_bytes / (elapsed_seconds > 0 ? elapsed_seconds : 1)))                                                                                                                      # Average throughput (avoid dividing by zero)
	TOTAL_PUSHES=$((TOTAL_PUSHES + 1))                                                                                                                                                                   # Count the push
	TOTAL_FILES_PUSHED=$((TOTAL_FILES_PUSHED + staged_file_count))                                                                                                                                       # Add the files to the total
	TOTAL_BYTES_PUSHED=$((TOTAL_BYTES_PUSHED + batch_bytes))                                                                                                                                             # Add the bytes to the total
	LAST_PUSH_EPOCH=$(date +%s)                                                                                                                                                                          # Restart the timer from the moment of the successful push
	log_message SUCCESS "PUSH COMPLETE: ${staged_file_count} file(s), $(format_bytes "$batch_bytes") in ${elapsed_seconds}s ($(format_bytes "$bytes_per_second")/s), HEAD $(git rev-parse --short HEAD)" # Report the result
	log_statistics                                                                                                                                                                                       # Show the updated totals
}                                                                                                                                                                                                     # End of push_batch

# ---------------- Main ----------------

function main() {                                                                                                                              # Program entry point
	reject_arguments "$@"                                                                                                                         # The script takes no arguments: anything passed is rejected here
	apply_options                                                                                                                                 # Combine environment variables and defaults, and validate every value
	enable_shell_trace_if_requested                                                                                                               # Print every shell command too, if SHELL_TRACE=1 was given
	log_message INFO "Auto git sync ${SCRIPT_VERSION} starting."                                                                                  # Log startup (console only)
	verify_dependencies                                                                                                                           # Make sure every tool exists
	acquire_single_instance_lock                                                                                                                  # Prevent duplicate instances
	enter_repository_root                                                                                                                         # Move into the repository
	verify_remote_access                                                                                                                          # Prove that pushing can work before doing any heavy work (retried on network failure)
	prepare_work_directory                                                                                                                        # Create the temporary directory
	local ignore_pattern                                                                                                                          # Loop variable for the local ignore rules
	for ignore_pattern in "*.part" "*.crdownload" "*.tmp" ".com.google.Chrome.*" ".org.chromium.Chromium.*" "**/assist_data/browser_profile/"; do # Half-finished downloads (downloader and Chrome), Chrome/Chromium temp names, and the Chrome profile folder with its caches and lock files
		add_local_ignore_rule "$ignore_pattern"                                                                                                      # Add the rule to .git/info/exclude (never committed); the files stay on disk
	done                                                                                                                                          # End of the ignore list
	start_following_extra_logs                                                                                                                    # Merge other apps' logs into this output, if configured
	log_all_options                                                                                                                               # Log EVERY option with its value, default, source and meaning
	log_environment_report                                                                                                                        # Log versions, settings and the starting state
	verify_free_disk_space                                                                                                                        # Check disk space once before starting
	STARTED_EPOCH=$(date +%s)                                                                                                                     # Record when the main loop starts
	LAST_PUSH_EPOCH=$STARTED_EPOCH                                                                                                                # Start the push timer now
	LAST_HEARTBEAT_EPOCH=$STARTED_EPOCH                                                                                                           # Start the heartbeat timer now

	while true; do                                                                       # Monitor the repository forever
		CYCLE_NUMBER=$((CYCLE_NUMBER + 1))                                                  # Count this cycle
		log_message TRACE "------ cycle ${CYCLE_NUMBER} begins ------"                      # Mark the start of the cycle in the output
		compress_pending_pdfs                                                               # Phase 1: compress and stage finished PDFs
		refresh_batch_counters                                                              # Update staged count and elapsed time
		if batch_is_ready; then                                                             # Phase 2: decide whether to push
			push_batch                                                                         # Commit, pull and push the batch (network failures are retried inside)
		fi                                                                                  # End of the push decision
		log_heartbeat_if_due                                                                # Log a status line once per heartbeat interval
		log_message TRACE "cycle ${CYCLE_NUMBER} done; sleeping ${CHECK_INTERVAL_SECONDS}s" # Log the pause
		sleep "${CHECK_INTERVAL_SECONDS}" &                                                 # Sleep in the background so a stop signal is handled immediately
		wait "$!" || true                                                                   # Wait for the sleep to finish (a signal interrupts this wait at once)
	done                                                                                 # End of the monitoring loop
}                                                                                     # End of main

main "$@" # Start the script

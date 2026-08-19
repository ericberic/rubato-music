-- Rubato's observable REAPER bridge.
--
-- Run this as a deferred ReaScript inside the machine-local "Rubato
-- Orchestra.RPP" project. It creates/repairs four monitored BBCSO tracks and
-- publishes a heartbeat consumed by Rubato's renderer-readiness endpoint.

local HOME = os.getenv("HOME")
local STATE_DIR = HOME .. "/Library/Application Support/Rubato/reaper"
local PROJECT_PATH = STATE_DIR .. "/Rubato Orchestra.RPP"
local HEARTBEAT_PATH = STATE_DIR .. "/renderer-status.json"
local LOG_PATH = STATE_DIR .. "/bridge.log"
-- Optional declarative per-section balance: a flat JSON object of
-- {"<track name>": <gain dB>} that the bridge applies to REAPER track faders
-- (D_VOL) whenever the file content changes. Lets section balance be set exactly
-- and reproducibly (e.g. lift the low strings) without hunting faders by hand.
local TRACK_GAIN_PATH = STATE_DIR .. "/track-gain.json"
local last_gain_signature = nil
local MIDI_INPUT_NAME = "Rubato Orchestra"
local FX_NAME = "VST3: BBC Symphony Orchestra (Spitfire Audio)"
-- BBCSO serializes an unconfigured instance as an XML <empty/> payload inside
-- REAPER's base64 VST chunk. A live FX alone is therefore not evidence that it
-- can make sound.
local EMPTY_BBCSO_STATE_MARKER = "PGVtcHR5Lz4"
local patch_probe_count = 0
-- One BBCSO track per Oguri orchestral section, each on its own MIDI channel so
-- every part plays a range- and timbre-correct Discover patch (the earlier
-- 4-track collapse routed basses onto cellos, bassoons/flutes onto clarinets).
-- The first four names are retained from the original 4-track rig so their
-- already-loaded patches are reused in place: "Low strings" now hosts Cellos and
-- "Woodwinds" now hosts Clarinets (legacy names, correct patch). The five new
-- tracks below need their Discover patch loaded once: 2nd Violins, Violas,
-- Basses, Flutes, Bassoons.
local TRACKS = {
  { name = "Rubato | Violins", channel = 1 },      -- 1st Violins (reused)
  { name = "Rubato | Violins II", channel = 2 },   -- 2nd Violins (load)
  { name = "Rubato | Violas", channel = 3 },        -- Violas (load)
  { name = "Rubato | Low strings", channel = 4 },  -- Cellos (reused, legacy name)
  { name = "Rubato | Basses", channel = 5 },        -- Basses (load)
  { name = "Rubato | Horns", channel = 6 },         -- Horns (reused)
  { name = "Rubato | Flutes", channel = 7 },        -- Flutes (load)
  { name = "Rubato | Woodwinds", channel = 8 },    -- Clarinets (reused, legacy name)
  { name = "Rubato | Bassoons", channel = 9 },      -- Bassoons (load)
}

local function json_escape(value)
  value = tostring(value or "")
  value = value:gsub("\\", "\\\\")
  value = value:gsub('"', '\\"')
  value = value:gsub("\n", "\\n")
  value = value:gsub("\r", "\\r")
  return value
end

local function json_string(value)
  if value == nil then return "null" end
  return '"' .. json_escape(value) .. '"'
end

local function write_heartbeat(state, message, values)
  reaper.RecursiveCreateDirectory(STATE_DIR, 0)
  local temp_path = HEARTBEAT_PATH .. ".tmp"
  local file = io.open(temp_path, "w")
  if not file then return end
  local body = string.format(
    '{"schema_version":1,"updated_at_epoch":%.3f,"state":%s,"message":%s,' ..
    '"project_path":%s,"midi_input_name":%s,"audio_output_name":%s,' ..
    '"sample_rate":%s,"block_size":%s,"output_latency_samples":%s,' ..
    '"ready_tracks":%d,"total_tracks":%d,' ..
    '"midi_event_sequence":%s,"midi_event_age_ms":%s,' ..
    '"midi_event_status":%s,"midi_event_data1":%s,"midi_event_data2":%s,' ..
    '"midi_events_seen":%d,"track_peak":%s,"master_peak":%s,' ..
    '"peak_since_start":%s}\n',
    os.time(), json_string(state), json_string(message),
    json_string(values.project_path), json_string(values.midi_input_name),
    json_string(values.audio_output_name), values.sample_rate or "null",
    values.block_size or "null", values.output_latency_samples or "null",
    values.ready_tracks or 0, #TRACKS,
    values.midi_event_sequence or "null", values.midi_event_age_ms or "null",
    values.midi_event_status or "null", values.midi_event_data1 or "null",
    values.midi_event_data2 or "null", values.midi_events_seen or 0,
    values.track_peak or "null",
    values.master_peak or "null", values.peak_since_start or "null"
  )
  file:write(body)
  file:close()
  os.rename(temp_path, HEARTBEAT_PATH)
end

local function append_log(message)
  reaper.RecursiveCreateDirectory(STATE_DIR, 0)
  local file = io.open(LOG_PATH, "a")
  if not file then return end
  file:write(os.date("!%Y-%m-%dT%H:%M:%SZ"), " ", tostring(message), "\n")
  file:close()
end

local function current_project_path()
  local _, path = reaper.EnumProjects(-1, "")
  return path or ""
end

local function rubato_input_candidates()
  -- REAPER keeps one cache row per CoreMIDI endpoint it has ever seen. Because
  -- Rubato's virtual source is process-owned, restarting the backend registers
  -- a *new* endpoint while the previous same-name row lingers as a stale ghost.
  -- Selecting the first name match (the old behavior) can therefore bind to a
  -- dead device index whose input history never receives the live probe. Return
  -- every same-name index instead and let event history pick the live one.
  local candidates = {}
  for index = 0, 63 do
    local present, name = reaper.GetMIDIInputName(index, "")
    if name == MIDI_INPUT_NAME then
      candidates[#candidates + 1] = { index = index, present = present and true or false }
    end
  end
  return candidates
end

local function track_by_name(name)
  for index = 0, reaper.CountTracks(0) - 1 do
    local track = reaper.GetTrack(0, index)
    local _, candidate = reaper.GetSetMediaTrackInfo_String(track, "P_NAME", "", false)
    if candidate == name then return track end
  end
  return nil
end

local function apply_track_gains()
  -- Apply the declarative per-section balance file to REAPER faders, but only
  -- when its content changes, so it never fights manual fader moves between
  -- edits. D_VOL is a linear multiplier: gain = 10^(dB/20).
  local file = io.open(TRACK_GAIN_PATH, "r")
  if not file then return end
  local content = file:read("*a")
  file:close()
  if content == last_gain_signature then return end
  last_gain_signature = content
  local applied = 0
  for name, db in content:gmatch('"([^"]+)"%s*:%s*(-?%d+%.?%d*)') do
    local track = track_by_name(name)
    if track ~= nil then
      local gain = 10 ^ (tonumber(db) / 20)
      reaper.SetMediaTrackInfo_Value(track, "D_VOL", gain)
      applied = applied + 1
      append_log(string.format("Applied section gain %.1f dB (x%.3f) to %s", tonumber(db), gain, name))
    end
  end
  if applied > 0 then reaper.Main_SaveProject(0, false) end
end

local function get_or_create_track(spec)
  local track = track_by_name(spec.name)
  if track then return track, false end
  reaper.InsertTrackAtIndex(reaper.CountTracks(0), true)
  track = reaper.GetTrack(0, reaper.CountTracks(0) - 1)
  reaper.GetSetMediaTrackInfo_String(track, "P_NAME", spec.name, true)
  return track, true
end

local function configure_track(track, spec, input_index)
  reaper.SetMediaTrackInfo_Value(track, "I_RECARM", 1)
  reaper.SetMediaTrackInfo_Value(track, "I_RECMON", 1)
  reaper.SetMediaTrackInfo_Value(track, "I_RECMODE", 0)
  reaper.SetMediaTrackInfo_Value(track, "B_MAINSEND", 1)
  -- MIDI flag + physical device in the next six bits + 1-based channel.
  reaper.SetMediaTrackInfo_Value(
    track, "I_RECINPUT", 4096 + input_index * 32 + spec.channel
  )
  local fx = reaper.TrackFX_GetInstrument(track)
  if fx < 0 then
    fx = reaper.TrackFX_AddByName(track, FX_NAME, false, 1)
    if fx < 0 then
      fx = reaper.TrackFX_AddByName(track, "BBC Symphony Orchestra", false, 1)
    end
  end
  return fx
end

local function fx_has_patch_state(track, fx)
  if fx < 0 or not reaper.TrackFX_GetEnabled(track, fx)
      or reaper.TrackFX_GetOffline(track, fx) then
    return false
  end
  local ok, chunk = reaper.GetTrackStateChunk(track, "", false)
  if not ok or chunk == nil then return false end
  local encoded_empty = chunk:find(EMPTY_BBCSO_STATE_MARKER, 1, true) ~= nil
  local literal_empty = chunk:find("<empty/>", 1, true) ~= nil
  if patch_probe_count < #TRACKS then
    patch_probe_count = patch_probe_count + 1
    append_log(
      string.format(
        "BBCSO state probe %d: bytes=%d encoded_empty=%s literal_empty=%s",
        patch_probe_count, #chunk, tostring(encoded_empty), tostring(literal_empty)
      )
    )
  end
  return not encoded_empty and not literal_empty
end

local function audio_values()
  local _, output = reaper.GetAudioDeviceInfo("IDENT_OUT")
  local _, sample_rate = reaper.GetAudioDeviceInfo("SRATE")
  local _, block_size = reaper.GetAudioDeviceInfo("BSIZE")
  local _, output_latency = reaper.GetInputOutputLatency()
  return output, sample_rate, block_size, output_latency
end

local initialized = false
local last_error = nil
local guarded_loop
local prompted_track_name = nil
local last_midi_sequence = nil
local midi_events_seen = 0
local peak_since_start = 0.0
-- Live device index carrying Rubato's CoreMIDI traffic. Latched from actual
-- received events so track binding and probe verification follow the real
-- endpoint rather than the first same-name cache row.
local active_input_index = nil
local logged_candidate_signature = nil
local last_device_census = nil
-- MIDI ingress self-heal. When Rubato's source is present but REAPER never
-- consumes its probe (a stale endpoint row after a backend restart, or a first
-- launch before the input is opened), the fix has been a manual "Reset all MIDI
-- devices". These latch that recovery so the bridge does it automatically, once
-- per endpoint appearance and globally rate-limited.
-- Grace is deliberately shorter than the Python probe window
-- (midi_probe_timeout_seconds) so a reset+rebind lands while CC119 is still being
-- resent, healing ingress within the same probe rather than needing another.
local RESET_GRACE_SECONDS = 3.0     -- silent grace before the first auto-reset
local RESET_MIN_INTERVAL_SECONDS = 5.0   -- global floor between any two auto-resets
local RESET_MAX_ATTEMPTS = 4        -- give up (until ingress recovers) after this many
local reset_cmd = nil               -- resolved+verified command id, or false if unavailable
local source_present_prev = false   -- was any same-name row present last loop
local midi_candidate_epoch = nil    -- reaper.time_precise() of the current source appearance
local ingress_confirmed = false     -- a NEW Rubato event arrived since this appearance
local ingress_baseline_sequence = 0 -- newest Rubato sequence at the appearance edge
local reset_done_this_appearance = false
local reset_attempts = 0            -- consecutive auto-resets since ingress last confirmed
local reset_last_time = -1e9        -- reaper.time_precise() of the last auto-reset
local reset_gave_up_logged = false

local function candidate_set(candidates)
  local set = {}
  for _, candidate in ipairs(candidates) do
    set[candidate.index] = true
  end
  return set
end

local function candidate_signature(candidates)
  local parts = {}
  for _, candidate in ipairs(candidates) do
    parts[#parts + 1] = string.format("%d/%s", candidate.index, tostring(candidate.present))
  end
  return table.concat(parts, ",")
end

local function choose_bind_index(candidates, set)
  -- Prefer the latched live index. Otherwise take the first "present" row, and
  -- fall back to the first candidate so tracks are still armed while we wait for
  -- the probe to reveal which same-name row is actually live.
  if active_input_index ~= nil and set[active_input_index] then
    return active_input_index
  end
  for _, candidate in ipairs(candidates) do
    if candidate.present then return candidate.index end
  end
  return candidates[1].index
end

local function log_device_census()
  -- Bounded forensic view: which device indices appear in REAPER's global input
  -- history right now. Logged only when the set changes, so it distinguishes
  -- "REAPER receives the probe on another index" from "no input arrives at all".
  local seen = {}
  local order = {}
  for index = 0, 63 do
    local sequence, _, _, device = reaper.MIDI_GetRecentInputEvent(index)
    if sequence == 0 then break end
    local dev = device % 65536
    if not seen[dev] then
      seen[dev] = true
      order[#order + 1] = dev
    end
  end
  table.sort(order)
  local census = table.concat(order, ",")
  if census ~= last_device_census then
    last_device_census = census
    append_log("Recent MIDI input device indices: [" .. census .. "]")
  end
end

local function recent_rubato_midi(set, sample_rate)
  -- MIDI_GetRecentInputEvent(0) latches REAPER's global input history. Walk
  -- backward until the newest event from *any* same-name Rubato row is found,
  -- latch that row as the live device index, and report the event. This
  -- distinguishes "Python sent MIDI" from "REAPER consumed MIDI" and, unlike the
  -- prior single-index filter, does not miss the probe when REAPER binds the
  -- live endpoint to a different same-name cache row than the first one listed.
  for index = 0, 63 do
    local sequence, message, timestamp, device = reaper.MIDI_GetRecentInputEvent(index)
    if sequence == 0 then break end
    local dev = device % 65536
    if set[dev] then
      if active_input_index ~= dev then
        active_input_index = dev
        append_log("Rubato active input latched to device index " .. dev)
      end
      local status = message and string.byte(message, 1) or nil
      if last_midi_sequence ~= sequence then
        last_midi_sequence = sequence
        midi_events_seen = midi_events_seen + 1
        append_log(
          string.format(
            "Rubato MIDI received: sequence=%d status=%s device=%d",
            sequence, tostring(status), dev
          )
        )
      end
      local age_ms = nil
      if sample_rate ~= nil and sample_rate > 0 then
        age_ms = math.max(0, -timestamp * 1000.0 / sample_rate)
      end
      return sequence, age_ms, status,
        message and string.byte(message, 2) or nil,
        message and string.byte(message, 3) or nil
    end
  end
  return nil, nil, nil, nil, nil
end

local function any_candidate_present(candidates)
  for _, candidate in ipairs(candidates) do
    if candidate.present then return true end
  end
  return false
end

local RESET_CMD_SCAN_LOW = 40000    -- native REAPER main-section action id range
local RESET_CMD_SCAN_HIGH = 50000

local function resolve_reset_command()
  -- Resolve REAPER's native "Reset all MIDI devices" action by NAME, never by a
  -- hardcoded number: kbd_getTextFromCmd reads an action's label without
  -- executing it, so this can never fire a wrong action and stays correct across
  -- REAPER versions where the numeric id differs. Scans the native id range once
  -- (cached). If nothing matches, auto-reset stays disabled and the manual action
  -- remains the recovery path.
  if reset_cmd ~= nil then return reset_cmd end
  if reaper.kbd_getTextFromCmd == nil or reaper.SectionFromUniqueID == nil then
    reset_cmd = false
    append_log("MIDI auto-reset unavailable: kbd_getTextFromCmd/SectionFromUniqueID missing")
    return reset_cmd
  end
  local main = reaper.SectionFromUniqueID(0)
  for cid = RESET_CMD_SCAN_LOW, RESET_CMD_SCAN_HIGH do
    local text = reaper.kbd_getTextFromCmd(cid, main)
    if text ~= nil and text:lower():find("reset all midi devices", 1, true) then
      reset_cmd = cid
      append_log(string.format("MIDI auto-reset action resolved: id=%d text=\"%s\"", cid, text))
      return reset_cmd
    end
  end
  reset_cmd = false
  append_log("MIDI auto-reset disabled: 'Reset all MIDI devices' action not found by name")
  return reset_cmd
end

local function maybe_reset_midi(candidates, newest_rubato_sequence)
  -- Fire one "Reset all MIDI devices" when the source is present and audio is
  -- running but REAPER consumes no *new* Rubato event after the grace period.
  --
  -- Detection keys off SOURCE PRESENCE, not the latched device index: a backend
  -- restart reopens the virtual source without changing REAPER's same-name cache
  -- rows, and REAPER's recent-input history still holds the pre-restart events,
  -- so the old latch would mask the new, unbound endpoint. On each present-edge
  -- (source (re)opened) we record the newest Rubato sequence as a baseline and
  -- require a sequence strictly greater than it to call ingress confirmed. Stale
  -- history therefore never counts, and an idle-but-healthy source (which was
  -- confirmed when its probe first landed) is never reset.
  local present = any_candidate_present(candidates)
  if present and not source_present_prev then
    midi_candidate_epoch = reaper.time_precise()
    reset_done_this_appearance = false
    ingress_confirmed = false
    ingress_baseline_sequence = newest_rubato_sequence or 0
  end
  source_present_prev = present
  if not present then return end
  if newest_rubato_sequence ~= nil
      and newest_rubato_sequence > ingress_baseline_sequence then
    ingress_confirmed = true
  end
  if ingress_confirmed then
    reset_attempts = 0
    reset_gave_up_logged = false
    return
  end
  if reaper.Audio_IsRunning() == 0 then return end
  if reset_done_this_appearance then return end
  local now = reaper.time_precise()
  if (now - (midi_candidate_epoch or now)) < RESET_GRACE_SECONDS then return end
  if (now - reset_last_time) < RESET_MIN_INTERVAL_SECONDS then return end
  if reset_attempts >= RESET_MAX_ATTEMPTS then
    -- A reset can itself briefly re-open the source (a fresh present-edge), so the
    -- per-appearance guard alone cannot stop a thrash loop; this cap does. Beyond
    -- it, only confirmed ingress re-arms auto-reset.
    if not reset_gave_up_logged then
      reset_gave_up_logged = true
      append_log(string.format(
        "MIDI auto-reset paused after %d attempts without ingress; manual check needed",
        reset_attempts
      ))
    end
    return
  end
  local cmd = resolve_reset_command()
  if not cmd then return end
  reset_done_this_appearance = true
  reset_last_time = now
  reset_attempts = reset_attempts + 1
  append_log(string.format(
    "Auto-resetting MIDI devices (attempt %d/%d): source present but no ingress after %.1fs",
    reset_attempts, RESET_MAX_ATTEMPTS, now - (midi_candidate_epoch or now)
  ))
  reaper.Main_OnCommand(cmd, 0)
end

local function loop()
  local project_path = current_project_path()
  local values = { project_path = project_path }
  if project_path ~= PROJECT_PATH then
    write_heartbeat(
      "setup_required",
      "Open the Rubato Orchestra.RPP project, then run the Rubato bridge",
      values
    )
    reaper.defer(guarded_loop)
    return
  end

  local candidates = rubato_input_candidates()
  if #candidates == 0 then
    active_input_index = nil
    write_heartbeat(
      "setup_required",
      "Waiting for Rubato's virtual MIDI source: " .. MIDI_INPUT_NAME,
      values
    )
    reaper.defer(guarded_loop)
    return
  end
  local signature = candidate_signature(candidates)
  if signature ~= logged_candidate_signature then
    logged_candidate_signature = signature
    append_log("Rubato MIDI input candidates (index/present): " .. signature)
  end
  local set = candidate_set(candidates)
  local input_index = choose_bind_index(candidates, set)
  values.midi_input_name = MIDI_INPUT_NAME

  local ready_tracks = 0
  local created_any = false
  local active_tracks = {}
  local first_unconfigured_track = nil
  local first_unconfigured_fx = -1
  local first_unconfigured_name = nil
  for _, spec in ipairs(TRACKS) do
    local track, created = get_or_create_track(spec)
    table.insert(active_tracks, track)
    local fx = configure_track(track, spec, input_index)
    created_any = created_any or created
    if fx_has_patch_state(track, fx) then
      ready_tracks = ready_tracks + 1
    elseif first_unconfigured_track == nil then
      first_unconfigured_track = track
      first_unconfigured_fx = fx
      first_unconfigured_name = spec.name
    end
  end
  values.ready_tracks = ready_tracks
  reaper.TrackList_AdjustWindows(false)
  if created_any then reaper.Main_SaveProject(0, false) end
  apply_track_gains()

  local output, sample_rate, block_size, output_latency = audio_values()
  values.audio_output_name = output
  values.sample_rate = tonumber(sample_rate)
  values.block_size = tonumber(block_size)
  values.output_latency_samples = tonumber(output_latency)
  log_device_census()
  local midi_sequence, midi_age_ms, midi_status, midi_data1, midi_data2 =
    recent_rubato_midi(set, values.sample_rate)
  values.midi_event_sequence = midi_sequence
  values.midi_event_age_ms = midi_age_ms
  values.midi_event_status = midi_status
  values.midi_event_data1 = midi_data1
  values.midi_event_data2 = midi_data2
  values.midi_events_seen = midi_events_seen
  -- Self-heal a silent ingress (stale endpoint after a backend restart, or a
  -- first launch before the input opens) without a manual "Reset all MIDI
  -- devices". Runs after recent_rubato_midi so it sees this loop's newest event.
  maybe_reset_midi(candidates, midi_sequence)
  local track_peak = 0.0
  for _, track in ipairs(active_tracks) do
    track_peak = math.max(
      track_peak,
      reaper.Track_GetPeakInfo(track, 0),
      reaper.Track_GetPeakInfo(track, 1)
    )
  end
  local master = reaper.GetMasterTrack(0)
  local master_peak = math.max(
    reaper.Track_GetPeakInfo(master, 0),
    reaper.Track_GetPeakInfo(master, 1)
  )
  peak_since_start = math.max(peak_since_start, track_peak, master_peak)
  values.track_peak = track_peak
  values.master_peak = master_peak
  values.peak_since_start = peak_since_start

  if ready_tracks < #TRACKS then
    write_heartbeat(
      "setup_required",
      string.format(
        "Choose a BBCSO patch for %s · %d of %d configured",
        first_unconfigured_name or "the next Rubato track", ready_tracks, #TRACKS
      ),
      values
    )
  elseif reaper.Audio_IsRunning() == 0 then
    write_heartbeat("failed", "REAPER's audio engine is not running", values)
  else
    write_heartbeat(
      "ready",
      string.format(
        "REAPER ready · %d tracks · %s samples @ %s Hz",
        ready_tracks, block_size or "?", sample_rate or "?"
      ),
      values
    )
    initialized = true
  end

  if first_unconfigured_track ~= nil and first_unconfigured_fx >= 0
      and prompted_track_name ~= first_unconfigured_name then
    -- Guide one patch at a time. Once BBCSO writes non-empty state, the next
    -- loop advances to the next track without any hidden modal instruction.
    prompted_track_name = first_unconfigured_name
    reaper.TrackFX_Show(first_unconfigured_track, first_unconfigured_fx, 3)
  elseif first_unconfigured_track == nil then
    prompted_track_name = nil
  end
  reaper.defer(guarded_loop)
end

guarded_loop = function()
  local ok, err = xpcall(loop, debug.traceback)
  if ok then return end
  last_error = tostring(err)
  local message = "Rubato bridge error: " .. last_error
  append_log(message)
  write_heartbeat(
    "failed", message,
    { project_path = current_project_path(), ready_tracks = initialized and #TRACKS or 0 }
  )
end

local function on_exit()
  if last_error ~= nil then return end
  write_heartbeat(
    "failed", "Rubato bridge stopped inside REAPER",
    { project_path = current_project_path(), ready_tracks = initialized and #TRACKS or 0 }
  )
end

append_log("Rubato bridge starting")
-- Verify (never fire) the auto-reset action at startup so the log states plainly
-- whether MIDI ingress self-heal is armed. kbd_getTextFromCmd only reads a label.
if resolve_reset_command() then
  append_log("MIDI ingress auto-reset armed (command id=" .. tostring(reset_cmd) .. ")")
end
-- REAPER recognizes the script as persistent only when the initial defer is
-- scheduled from the top-level chunk. Calling a function that later defers is
-- not sufficient on this REAPER build: the chunk exits and atexit fires.
reaper.defer(guarded_loop)
reaper.atexit(on_exit)

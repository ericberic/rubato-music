-- One-shot: turn Rubato's stereo orchestra into an 8-channel room feed.
-- Master -> 8 ch, (re)insert the Rubato surround upmix, route ch3-8 to hardware
-- outputs 3-8. Removes any prior copy first so edits to the JSFX recompile.
local HOME = os.getenv("HOME")
local LOG = HOME .. "/Library/Application Support/Rubato/reaper/bridge.log"
local function log(s)
  local f = io.open(LOG, "a")
  if f then f:write(os.date("!%Y-%m-%dT%H:%M:%SZ") .. " SURROUND " .. s .. "\n"); f:close() end
end

local m = reaper.GetMasterTrack(0)
reaper.SetMediaTrackInfo_Value(m, "I_NCHAN", 8)

-- remove any existing upmix so the edited JSFX reloads fresh
local existing = reaper.TrackFX_AddByName(m, "rubato_upmix_51", false, 0)
while existing >= 0 do
  reaper.TrackFX_Delete(m, existing)
  existing = reaper.TrackFX_AddByName(m, "rubato_upmix_51", false, 0)
end
local fx = reaper.TrackFX_AddByName(m, "rubato_upmix_51", false, 1)
log("master set to 8ch, upmix fx index=" .. tostring(fx))

-- rebuild hardware output sends for channels 3..8 (0-based 2..7), mono 1:1
for i = reaper.GetTrackNumSends(m, 1) - 1, 0, -1 do
  reaper.RemoveTrackSend(m, 1, i)
end
for ch = 2, 7 do
  local idx = reaper.CreateTrackSend(m, nil)
  reaper.SetTrackSendInfo_Value(m, 1, idx, "I_SRCCHAN", ch + 1024)
  reaper.SetTrackSendInfo_Value(m, 1, idx, "I_DSTCHAN", ch + 1024)
  log("hardware send: master ch" .. (ch + 1) .. " -> hw out " .. (ch + 1))
end

reaper.Main_SaveProject(0, false)
reaper.ShowConsoleMsg("Rubato surround upmix: Master=8ch, ch3-8 -> hardware out 3-8.\n")

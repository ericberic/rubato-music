-- Rubato managed REAPER startup bridge.
-- REAPER automatically runs Scripts/__startup.lua. Loading the bridge here
-- keeps its initial reaper.defer() in the startup script's top-level context.

local bridge_path = reaper.GetResourcePath()
  .. "/Scripts/Rubato/rubato_reaper_bridge.lua"
dofile(bridge_path)

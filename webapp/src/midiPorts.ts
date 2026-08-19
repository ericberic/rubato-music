/**
 * MIDI port selection and hardware identification helpers.
 */

const HARDWARE_KEYWORD_SUBSTRINGS = [
  'clavinova',
  'yamaha',
  'clp',
  'keyboard',
  'piano',
  'usb midi',
  'usb-midi',
  'digital piano',
  'roland',
  'kawai',
  'casio',
  'nord',
  'korg',
  'arturia',
  'studiologic',
  'privia',
  'numa',
  'widi',
  'um-one',
  'dexibell',
  'kurzweil',
];

const VIRTUAL_BUS_SUBSTRINGS = [
  'iac driver',
  'midi through',
  'virtual',
  'pianoteq',
  'keyscape',
  'ableton',
  'mainstage',
  'kontakt',
  'loopmidi',
  'rtpmidi',
  'bus',
  'synth',
];

export function isHardwarePianoPort(name: string): boolean {
  if (!name) return false;
  const lower = name.toLowerCase();
  if (VIRTUAL_BUS_SUBSTRINGS.some((v) => lower.includes(v))) {
    return false;
  }
  return HARDWARE_KEYWORD_SUBSTRINGS.some((k) => lower.includes(k));
}

export function pickBestPort(
  available: string[],
  current: string,
  preferred: string,
): string {
  // A momentarily empty device list must not destroy the performer's choice.
  // The Clavinova drops off CoreMIDI when it sleeps or is re-plugged, and
  // clearing the selection here silently disabled the Record buttons, which
  // then appeared to flicker between enabled and disabled with no visible
  // cause. Retaining it means a genuinely absent device fails loudly at start
  // time with a real error instead of a mystery grey button.
  if (!available.length) return current;

  // 1. Honor explicit user preference if present
  if (preferred && available.includes(preferred)) {
    return preferred;
  }

  // 2. Retain current selection if valid hardware
  if (current && available.includes(current) && isHardwarePianoPort(current)) {
    return current;
  }

  // 3. Promote newly discovered physical piano hardware
  const hardware = available.find(isHardwarePianoPort);
  if (hardware) {
    return hardware;
  }

  // 4. Retain existing selection if valid
  if (current && available.includes(current)) {
    return current;
  }

  // 5. Fallback to first available port
  return available[0];
}

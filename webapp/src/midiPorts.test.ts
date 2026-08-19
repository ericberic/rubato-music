import { describe, expect, it } from 'vitest';
import { isHardwarePianoPort, pickBestPort } from './midiPorts';

describe('midiPorts helpers', () => {
  describe('isHardwarePianoPort', () => {
    it('identifies major digital piano hardware manufacturers', () => {
      expect(isHardwarePianoPort('Clavinova')).toBe(true);
      expect(isHardwarePianoPort('Yamaha CLP-795GP')).toBe(true);
      expect(isHardwarePianoPort('Roland FP-30X')).toBe(true);
      expect(isHardwarePianoPort('Roland RD-2000')).toBe(true);
      expect(isHardwarePianoPort('Kawai MP11SE')).toBe(true);
      expect(isHardwarePianoPort('Kawai VPC1')).toBe(true);
      expect(isHardwarePianoPort('Casio Privia PX-S1100')).toBe(true);
      expect(isHardwarePianoPort('Nord Stage 3')).toBe(true);
      expect(isHardwarePianoPort('Arturia KeyLab 88')).toBe(true);
      expect(isHardwarePianoPort('UM-ONE')).toBe(true);
      expect(isHardwarePianoPort('WIDI Master')).toBe(true);
    });

    it('rejects virtual buses and software synths', () => {
      expect(isHardwarePianoPort('IAC Driver Bus 1')).toBe(false);
      expect(isHardwarePianoPort('Midi Through Port-0')).toBe(false);
      expect(isHardwarePianoPort('Pianoteq 8')).toBe(false);
      expect(isHardwarePianoPort('Keyscape Piano')).toBe(false);
      expect(isHardwarePianoPort('Ableton Live Piano Bus')).toBe(false);
      expect(isHardwarePianoPort('MainStage Piano Output')).toBe(false);
      expect(isHardwarePianoPort('Kontakt 7 Piano Out')).toBe(false);
      expect(isHardwarePianoPort('loopMIDI Piano')).toBe(false);
      expect(isHardwarePianoPort('')).toBe(false);
    });
  });

  describe('pickBestPort', () => {
    it('keeps the existing selection when no ports are listed', () => {
      // An empty poll is usually a transient CoreMIDI dropout, not a real
      // removal; discarding the selection silently disabled Record.
      expect(pickBestPort([], 'Clavinova', 'Yamaha')).toBe('Clavinova');
      expect(pickBestPort([], '', 'Yamaha')).toBe('');
    });

    it('honors explicit user preference even if virtual', () => {
      const ports = ['IAC Driver Bus 1', 'Clavinova'];
      expect(pickBestPort(ports, '', 'IAC Driver Bus 1')).toBe('IAC Driver Bus 1');
      expect(pickBestPort(ports, '', 'Clavinova')).toBe('Clavinova');
    });

    it('promotes physical hardware over virtual fallback when no explicit preference', () => {
      const ports = ['IAC Driver Bus 1', 'Clavinova'];
      expect(pickBestPort(ports, 'IAC Driver Bus 1', '')).toBe('Clavinova');
    });

    it('retains valid hardware selection', () => {
      const ports = ['IAC Driver Bus 1', 'Clavinova'];
      expect(pickBestPort(ports, 'Clavinova', '')).toBe('Clavinova');
    });

    it('falls back to first port when no hardware exists', () => {
      const ports = ['Virtual Port A', 'Virtual Port B'];
      expect(pickBestPort(ports, '', '')).toBe('Virtual Port A');
    });
  });
});

describe('transient device dropouts', () => {
  it('keeps the current selection when the device list is momentarily empty', () => {
    // The Clavinova drops off CoreMIDI when it sleeps or is re-plugged. Clearing
    // the selection silently disabled the Record buttons, so they appeared to
    // flicker between enabled and disabled with no visible cause.
    expect(pickBestPort([], 'Clavinova', '')).toBe('Clavinova');
    expect(pickBestPort([], '', '')).toBe('');
  });

  it('restores normal selection once the port list comes back', () => {
    let selected = pickBestPort(['Clavinova'], '', '');
    expect(selected).toBe('Clavinova');
    selected = pickBestPort([], selected, '');
    expect(selected).toBe('Clavinova');
    selected = pickBestPort(['Clavinova'], selected, '');
    expect(selected).toBe('Clavinova');
  });
});

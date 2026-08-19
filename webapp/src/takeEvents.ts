// Client for `WS /api/events` (design doc §2.7): pushes take status
// transitions so the capture cockpit can resolve the after-take toast and
// re-arm without polling (design doc §3.1). Deliberately minimal -- one
// socket, reconnect-with-backoff, no message queue/replay (a reconnecting
// client just re-fetches current state via the existing REST endpoints,
// same as App.svelte already does on mount).
//
// The payload contract (`TakeEvent`, a discriminated union on `type`) is
// generated from the backend's Pydantic models (design doc §2.1/§2.2, issues
// #85/#83/#84): `aimusic.server.schemas.TakeEvent` is injected into the
// OpenAPI document's `components.schemas` (there's no HTTP request/response
// to hang it off, since this is a plain WebSocket route), so the same
// `@hey-api/openapi-ts` pass that generates the REST client's types and Zod
// schemas also covers this socket. Messages are parsed with the generated
// Zod schema, not a bare `as TakeEvent` assertion -- a malformed message is
// a "fail loudly" case (design doc §2.2), not a value to trust.

import { zTakeEvent } from './generated/zod.gen';
import type { TakeEvent } from './generated';

export type { TakeEvent };

export type TakeEventsStatus = 'connecting' | 'open' | 'closed';

const INITIAL_RETRY_DELAY_MS = 1000;
const MAX_RETRY_DELAY_MS = 15000;

/** Test/debug introspection only -- not read by any production code path. */
export type TakeEventsDebugHandle = {
  status: TakeEventsStatus;
  reconnectCount: number;
  /** Forcibly drops the live socket, e.g. to test reconnect-with-backoff. */
  forceDisconnect: () => void;
};

export function connectTakeEvents(
  onEvent: (event: TakeEvent) => void,
  onParseError?: (error: unknown) => void,
  onOpen?: () => void,
): () => void {
  let socket: WebSocket | null = null;
  let closedByCaller = false;
  let retryDelayMs = INITIAL_RETRY_DELAY_MS;
  let retryTimer: ReturnType<typeof setTimeout> | null = null;
  let reconnectCount = 0;

  const debugHandle: TakeEventsDebugHandle = {
    status: 'connecting',
    reconnectCount: 0,
    forceDisconnect: () => socket?.close(),
  };
  if (typeof window !== 'undefined') {
    (window as unknown as { __rubatoTakeEvents?: TakeEventsDebugHandle }).__rubatoTakeEvents =
      debugHandle;
  }

  function setStatus(status: TakeEventsStatus) {
    debugHandle.status = status;
  }

  function connect() {
    setStatus('connecting');
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    socket = new WebSocket(`${protocol}//${window.location.host}/api/events`);

    socket.onopen = () => {
      retryDelayMs = INITIAL_RETRY_DELAY_MS;
      setStatus('open');
      onOpen?.();
    };

    socket.onmessage = (event: MessageEvent<string>) => {
      try {
        onEvent(zTakeEvent.parse(JSON.parse(event.data)));
      } catch (error) {
        console.error('Malformed take event from /api/events', error);
        onParseError?.(error);
      }
    };

    socket.onclose = () => {
      setStatus('closed');
      if (closedByCaller) return;
      reconnectCount += 1;
      debugHandle.reconnectCount = reconnectCount;
      retryTimer = setTimeout(connect, retryDelayMs);
      retryDelayMs = Math.min(retryDelayMs * 2, MAX_RETRY_DELAY_MS);
    };

    socket.onerror = () => {
      // onclose always follows onerror for a WebSocket; the reconnect is
      // scheduled there, so this just avoids an unhandled-error console spew.
      socket?.close();
    };
  }

  connect();

  return function disconnect() {
    closedByCaller = true;
    if (retryTimer) {
      clearTimeout(retryTimer);
      retryTimer = null;
    }
    socket?.close();
  };
}

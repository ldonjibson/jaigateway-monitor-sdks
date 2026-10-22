export interface ReplayOptions {
  /** Share of visitor sessions recorded from the start (0 to 1, default 0.1). */
  sampleRate?: number;
  /** Of the other sessions, the share that keep the last ~30-60 s in memory and upload them (with everything after) when an error is reported (default 1). */
  errorSampleRate?: number;
  /** Mask all text on the page (default true). Elements inside `.insight-unmask` (or `unmaskSelector`) stay readable. */
  maskAllText?: boolean;
  /** Hide the content of every input (default true). Passwords are always hidden. */
  maskAllInputs?: boolean;
  /** CSS selector of elements removed from the recording entirely (also: class `insight-block`). */
  blockSelector?: string;
  /** With maskAllText false: extra elements whose text is masked. */
  maskSelector?: string;
  unmaskSelector?: string;
  /** Stop after this long (default 30 min, at most 60 min). */
  maxDurationMs?: number;
  /** Do not record until Insight.startReplay() is called (use after the visitor consents). */
  manualStart?: boolean;
}
export function enableReplay(api: unknown, cfg?: ReplayOptions): unknown;

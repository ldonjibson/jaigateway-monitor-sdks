/** The protocol's fixed latency histogram (sdks/PROTOCOL.md): shared by every SDK. */

export const BOUNDS_MS = [1, 2, 3, 5, 8, 12, 18, 25, 40, 60, 90, 130, 200, 300, 450, 700, 1000, 1500, 2200, 3300, 5000, 7500, 11000, 17000, 25000, 40000, 60000];
export function bucketIndex(ms) {
  for (let i = 0; i < BOUNDS_MS.length; i++) if (ms <= BOUNDS_MS[i]) return i;
  return BOUNDS_MS.length;
}

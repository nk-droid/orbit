/**
 * How Orbit's microphone is opened, in a conversation and when listening for its name.
 *
 * Orbit must not hear what the computer itself is playing. Left to the default, the browser
 * only has to remove Orbit's own voice from the microphone, so music or a video playing in
 * another tab comes through and Orbit takes it for the user speaking. Asking for "all" has
 * the browser remove everything this computer plays.
 * See https://developer.mozilla.org/docs/Web/API/MediaTrackConstraints/echoCancellation
 */
// TypeScript's typings only know true and false here, so the newer value is passed as one.
export const HEAR_ONLY_THE_ROOM = { echoCancellation: 'all' as unknown as boolean }

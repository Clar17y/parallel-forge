let token: string | null = null;
const invalidated = new Set<() => void>();
export const csrf = {
  get: () => token,
  set: (value: string) => { token = value; },
  clear: () => {
    token = null;
    for (const listener of invalidated) listener();
  },
  onInvalidated: (listener: () => void) => {
    invalidated.add(listener);
    return () => { invalidated.delete(listener); };
  },
};

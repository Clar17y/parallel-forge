'use client';

import { createContext, useContext } from 'react';

export const SessionIdentity = createContext<string | null>(null);
export const useSessionIdentity = () => useContext(SessionIdentity);

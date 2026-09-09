import { useSyncExternalStore } from 'react';
import Flashbar, { type FlashbarProps } from '@cloudscape-design/components/flashbar';

let items: FlashbarProps.MessageDefinition[] = [];
let nextId = 0;
const listeners = new Set<() => void>();
const subscribe = (listener: () => void) => {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
};
const snapshot = () => items;
const emit = () => listeners.forEach(listener => listener());

function add(type: 'success' | 'error', header: string, options?: { description?: string }) {
  const id = `console-notification-${++nextId}`;
  items = [...items.slice(-2), {
    id, type, header, content: options?.description,
    dismissible: true, dismissLabel: 'Dismiss notification',
    onDismiss: () => { items = items.filter(item => item.id !== id); emit(); },
  }];
  emit();
}

export const toast = {
  success: (message: string, options?: { description?: string }) => add('success', message, options),
  error: (message: string, options?: { description?: string }) => add('error', message, options),
};

export function ConsoleNotifications() {
  return <Flashbar items={useSyncExternalStore(subscribe, snapshot)} />;
}

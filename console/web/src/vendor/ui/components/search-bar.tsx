import { type RefObject } from 'react';
import { Search, X } from 'lucide-react';
import { Input } from '@foxl/ui';
import { Button } from '@foxl/ui';
import { cn } from '@foxl/ui';

interface SearchBarProps {
  isOpen: boolean;
  query: string;
  onQueryChange: (q: string) => void;
  onClose: () => void;
  inputRef: RefObject<HTMLInputElement | null>;
  resultCount?: number;
  placeholder?: string;
  className?: string;
}

export function SearchBar({
  isOpen,
  query,
  onQueryChange,
  onClose,
  inputRef,
  resultCount,
  placeholder = 'Search…',
  className,
}: SearchBarProps) {
  if (!isOpen) return null;

  return (
    <div className={cn(
      'flex items-center gap-2 px-3 py-2 border-b bg-background/95 backdrop-blur-sm',
      className,
    )}>
      <Search className="h-3.5 w-3.5 text-muted-foreground shrink-0" aria-hidden="true" />
      <Input
        ref={inputRef as RefObject<HTMLInputElement>}
        value={query}
        onChange={e => onQueryChange(e.target.value)}
        placeholder={placeholder}
        className="h-7 text-sm border-0 shadow-none focus-visible:ring-0 px-0"
        spellCheck={false}
        autoComplete="off"
        aria-label="Search"
      />
      {query && resultCount !== undefined && (
        <span className="text-xs text-muted-foreground whitespace-nowrap font-variant-numeric tabular-nums">
          {resultCount} found
        </span>
      )}
      <Button
        variant="ghost"
        size="icon"
        className="h-6 w-6 shrink-0"
        onClick={onClose}
        aria-label="Close search"
      >
        <X className="h-3.5 w-3.5" aria-hidden="true" />
      </Button>
    </div>
  );
}

import { Sun, Moon, Monitor } from "lucide-react";
import {
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
} from "../dropdown-menu";
import { cn } from "../../lib/utils";

export type ThemeMode = "light" | "dark" | "system" | (string & {});

export interface ThemeSwitcherItemsProps {
  theme: ThemeMode | undefined;
  setTheme: (next: ThemeMode) => void;
  /** Optional translated labels - defaults to English */
  labels?: {
    title?: string;
    light?: string;
    dark?: string;
    system?: string;
  };
}

/**
 * Theme switcher items meant to live inside an open <DropdownMenuContent>.
 * Used by both apps/web and web account menus so the theme UX
 * is identical across products. Caller owns the dropdown trigger + content.
 */
export function ThemeSwitcherItems({
  theme,
  setTheme,
  labels,
}: ThemeSwitcherItemsProps) {
  const l = {
    title: labels?.title ?? "Theme",
    light: labels?.light ?? "Light",
    dark: labels?.dark ?? "Dark",
    system: labels?.system ?? "System",
  };
  return (
    <>
      <DropdownMenuLabel className="text-xs font-normal text-muted-foreground">
        {l.title}
      </DropdownMenuLabel>
      <DropdownMenuItem onClick={() => setTheme("light")}>
        <Sun className="h-4 w-4" />
        <span>{l.light}</span>
        <ThemeDot active={theme === "light"} />
      </DropdownMenuItem>
      <DropdownMenuItem onClick={() => setTheme("dark")}>
        <Moon className="h-4 w-4" />
        <span>{l.dark}</span>
        <ThemeDot active={theme === "dark"} />
      </DropdownMenuItem>
      <DropdownMenuItem onClick={() => setTheme("system")}>
        <Monitor className="h-4 w-4" />
        <span>{l.system}</span>
        <ThemeDot active={theme === "system"} />
      </DropdownMenuItem>
      <DropdownMenuSeparator />
    </>
  );
}

function ThemeDot({ active }: { active: boolean }) {
  return (
    <span
      className={cn(
        "ml-auto h-1.5 w-1.5 rounded-full transition-colors",
        active ? "bg-foreground" : "bg-transparent",
      )}
    />
  );
}

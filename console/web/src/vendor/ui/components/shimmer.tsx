"use client"

import { type CSSProperties, type ElementType, type JSX, memo, useMemo } from "react"
import { cn } from "@foxl/ui"

export interface ShimmerProps {
  children: string
  as?: ElementType
  className?: string
  duration?: number
  spread?: number
}

const ShimmerComponent = ({
  children,
  as: Component = "span",
  className,
  duration = 2,
  spread = 2,
}: ShimmerProps) => {
  const dynamicSpread = useMemo(() => (children?.length ?? 0) * spread, [children, spread])

  return (
    <Component
      className={cn(
        "relative inline-block bg-[length:250%_100%,auto] bg-clip-text text-transparent animate-shimmer",
        "[background-repeat:no-repeat,padding-box]",
        className,
      )}
      style={{
        "--spread": `${dynamicSpread}px`,
        // Both keyframe variants in the codebase read `--duration`
        // (apps/web/index.css), but web/src/styles.css uses
        // `--tw-animate-duration`. Set both so the shimmer runs at the
        // requested duration regardless of which CSS gets bundled.
        "--duration": `${duration}s`,
        "--tw-animate-duration": `${duration}s`,
        animationDuration: `${duration}s`,
        backgroundImage:
          `linear-gradient(90deg, #0000 calc(50% - ${dynamicSpread}px), hsl(var(--foreground)), #0000 calc(50% + ${dynamicSpread}px)), linear-gradient(hsl(var(--muted-foreground)), hsl(var(--muted-foreground)))`,
      } as CSSProperties}
    >
      {children}
    </Component>
  )
}

export const Shimmer = memo(ShimmerComponent)

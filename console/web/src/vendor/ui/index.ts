// @foxl/ui - shared shadcn/ui components for the desktop app and the code app
//
// All components are framework-agnostic className-based primitives. They work
// with Tailwind v3 (consumer maps HSL CSS vars in tailwind.config.js) and
// Tailwind v4 (consumer uses @theme inline). See README for setup.

export * from "./lib/utils";

export * from "./components/accordion";
export * from "./components/alert";
export * from "./components/alert-dialog";
export * from "./components/avatar";
export * from "./components/badge";
export * from "./components/button";
export * from "./components/card";
// chart is a heavy-deps module (recharts). Import via "@foxl/ui/chart"
// instead of the root barrel so consumers that don't use it don't pay
// for recharts in node_modules resolution.
// export * from "./components/chart";
export * from "./components/collapsible";
export * from "./components/command";
export * from "./components/context-menu";
export * from "./components/dialog";
export * from "./components/dropdown-menu";
export * from "./components/hover-card";
export * from "./components/input";
export * from "./components/label";
export * from "./components/popover";
export * from "./components/progress";
export * from "./components/resizable";
export * from "./components/scroll-area";
export * from "./components/search-bar";
export * from "./components/select";
export * from "./components/separator";
export * from "./components/sheet";
export * from "./components/shimmer";
export * from "./components/sidebar";
export * from "./components/skeleton";
export * from "./components/sonner";
export * from "./components/spinner";
export * from "./components/switch";
export * from "./components/table";
export * from "./components/tabs";
export * from "./components/textarea";
export * from "./components/toggle";
export * from "./components/tooltip";

// Shared layout primitives (used by both apps/web and web)
export * from "./components/layout/brand-header";
export * from "./components/layout/theme-switcher";
export * from "./components/layout/app-shell";
export * from "./components/layout/nav-sidebar";
export * from "./components/layout/top-bar";
export * from "./components/layout/breadcrumbs";

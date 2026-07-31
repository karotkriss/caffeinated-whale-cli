import React from "react";

// Icon paths copied verbatim from Lucide (https://lucide.dev, ISC licence).
// Source SVGs also live in assets/icons/ — regenerate this map from those files.
export const ICON_PATHS = {
  "activity": '<path d="M22 12h-2.48a2 2 0 0 0-1.93 1.46l-2.35 8.36a.25.25 0 0 1-.48 0L9.24 2.18a.25.25 0 0 0-.48 0l-2.35 8.36A2 2 0 0 1 4.49 12H2"></path>',
  "arrow-up-right": '<path d="M7 7h10v10"></path> <path d="M7 17 17 7"></path>',
  "box": '<path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z"></path> <path d="m3.3 7 8.7 5 8.7-5"></path> <path d="M12 22V12"></path>',
  "check": '<path d="M20 6 9 17l-5-5"></path>',
  "chevron-down": '<path d="m6 9 6 6 6-6"></path>',
  "chevron-right": '<path d="m9 18 6-6-6-6"></path>',
  "circle-alert": '<circle cx="12" cy="12" r="10"></circle> <line x1="12" x2="12" y1="8" y2="12"></line> <line x1="12" x2="12.01" y1="16" y2="16"></line>',
  "circle-check": '<circle cx="12" cy="12" r="10"></circle> <path d="m9 12 2 2 4-4"></path>',
  "circle-dot": '<circle cx="12" cy="12" r="10"></circle> <circle cx="12" cy="12" r="1"></circle>',
  "clock": '<circle cx="12" cy="12" r="10"></circle> <path d="M12 6v6l4 2"></path>',
  "coffee": '<path d="M10 2v2"></path> <path d="M14 2v2"></path> <path d="M16 8a1 1 0 0 1 1 1v8a4 4 0 0 1-4 4H7a4 4 0 0 1-4-4V9a1 1 0 0 1 1-1h14a4 4 0 1 1 0 8h-1"></path> <path d="M6 2v2"></path>',
  "copy": '<rect width="14" height="14" x="8" y="8" rx="2" ry="2"></rect> <path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"></path>',
  "cpu": '<path d="M12 20v2"></path> <path d="M12 2v2"></path> <path d="M17 20v2"></path> <path d="M17 2v2"></path> <path d="M2 12h2"></path> <path d="M2 17h2"></path> <path d="M2 7h2"></path> <path d="M20 12h2"></path> <path d="M20 17h2"></path> <path d="M20 7h2"></path> <path d="M7 20v2"></path> <path d="M7 2v2"></path> <rect x="4" y="4" width="16" height="16" rx="2"></rect> <rect x="8" y="8" width="8" height="8" rx="1"></rect>',
  "database": '<ellipse cx="12" cy="5" rx="9" ry="3"></ellipse> <path d="M3 5V19A9 3 0 0 0 21 19V5"></path> <path d="M3 12A9 3 0 0 0 21 12"></path>',
  "download": '<path d="M12 15V3"></path> <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path> <path d="m7 10 5 5 5-5"></path>',
  "ellipsis": '<circle cx="12" cy="12" r="1"></circle> <circle cx="19" cy="12" r="1"></circle> <circle cx="5" cy="12" r="1"></circle>',
  "external-link": '<path d="M15 3h6v6"></path> <path d="M10 14 21 3"></path> <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"></path>',
  "file-text": '<path d="M6 22a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h8a2.4 2.4 0 0 1 1.704.706l3.588 3.588A2.4 2.4 0 0 1 20 8v12a2 2 0 0 1-2 2z"></path> <path d="M14 2v5a1 1 0 0 0 1 1h5"></path> <path d="M10 9H8"></path> <path d="M16 13H8"></path> <path d="M16 17H8"></path>',
  "folder": '<path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"></path>',
  "git-branch": '<path d="M15 6a9 9 0 0 0-9 9V3"></path> <circle cx="18" cy="6" r="3"></circle> <circle cx="6" cy="18" r="3"></circle>',
  "globe": '<circle cx="12" cy="12" r="10"></circle> <path d="M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20"></path> <path d="M2 12h20"></path>',
  "hard-drive": '<path d="M10 16h.01"></path> <path d="M2.212 11.577a2 2 0 0 0-.212.896V18a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-5.527a2 2 0 0 0-.212-.896L18.55 5.11A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"></path> <path d="M21.946 12.013H2.054"></path> <path d="M6 16h.01"></path>',
  "info": '<circle cx="12" cy="12" r="10"></circle> <path d="M12 16v-4"></path> <path d="M12 8h.01"></path>',
  "layers": '<path d="M12.83 2.18a2 2 0 0 0-1.66 0L2.6 6.08a1 1 0 0 0 0 1.83l8.58 3.91a2 2 0 0 0 1.66 0l8.58-3.9a1 1 0 0 0 0-1.83z"></path> <path d="M2 12a1 1 0 0 0 .58.91l8.6 3.91a2 2 0 0 0 1.65 0l8.58-3.9A1 1 0 0 0 22 12"></path> <path d="M2 17a1 1 0 0 0 .58.91l8.6 3.91a2 2 0 0 0 1.65 0l8.58-3.9A1 1 0 0 0 22 17"></path>',
  "loader-circle": '<path d="M21 12a9 9 0 1 1-6.219-8.56"></path>',
  "lock": '<rect width="18" height="11" x="3" y="11" rx="2" ry="2"></rect> <path d="M7 11V7a5 5 0 0 1 10 0v4"></path>',
  "lock-open": '<rect width="18" height="11" x="3" y="11" rx="2" ry="2"></rect> <path d="M7 11V7a5 5 0 0 1 9.9-1"></path>',
  "minus": '<path d="M5 12h14"></path>',
  "package": '<path d="M11 21.73a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73z"></path> <path d="M12 22V12"></path> <polyline points="3.29 7 12 12 20.71 7"></polyline> <path d="m7.5 4.27 9 5.15"></path>',
  "panel-left": '<rect width="18" height="18" x="3" y="3" rx="2"></rect> <path d="M9 3v18"></path>',
  "pencil": '<path d="M21.174 6.812a1 1 0 0 0-3.986-3.987L3.842 16.174a2 2 0 0 0-.5.83l-1.321 4.352a.5.5 0 0 0 .623.622l4.353-1.32a2 2 0 0 0 .83-.497z"></path> <path d="m15 5 4 4"></path>',
  "play": '<path d="M5 5a2 2 0 0 1 3.008-1.728l11.997 6.998a2 2 0 0 1 .003 3.458l-12 7A2 2 0 0 1 5 19z"></path>',
  "plus": '<path d="M5 12h14"></path> <path d="M12 5v14"></path>',
  "refresh-cw": '<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"></path> <path d="M21 3v5h-5"></path> <path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"></path> <path d="M8 16H3v5"></path>',
  "rotate-cw": '<path d="M21 12a9 9 0 1 1-9-9c2.52 0 4.93 1 6.74 2.74L21 8"></path> <path d="M21 3v5h-5"></path>',
  "scroll-text": '<path d="M15 12h-5"></path> <path d="M15 8h-5"></path> <path d="M19 17V5a2 2 0 0 0-2-2H4"></path> <path d="M8 21h12a2 2 0 0 0 2-2v-1a1 1 0 0 0-1-1H11a1 1 0 0 0-1 1v1a2 2 0 1 1-4 0V5a2 2 0 1 0-4 0v2a1 1 0 0 0 1 1h3"></path>',
  "search": '<path d="m21 21-4.34-4.34"></path> <circle cx="11" cy="11" r="8"></circle>',
  "server": '<rect width="20" height="8" x="2" y="2" rx="2" ry="2"></rect> <rect width="20" height="8" x="2" y="14" rx="2" ry="2"></rect> <line x1="6" x2="6.01" y1="6" y2="6"></line> <line x1="6" x2="6.01" y1="18" y2="18"></line>',
  "settings": '<path d="M9.671 4.136a2.34 2.34 0 0 1 4.659 0 2.34 2.34 0 0 0 3.319 1.915 2.34 2.34 0 0 1 2.33 4.033 2.34 2.34 0 0 0 0 3.831 2.34 2.34 0 0 1-2.33 4.033 2.34 2.34 0 0 0-3.319 1.915 2.34 2.34 0 0 1-4.659 0 2.34 2.34 0 0 0-3.32-1.915 2.34 2.34 0 0 1-2.33-4.033 2.34 2.34 0 0 0 0-3.831A2.34 2.34 0 0 1 6.35 6.051a2.34 2.34 0 0 0 3.319-1.915"></path> <circle cx="12" cy="12" r="3"></circle>',
  "square": '<rect width="18" height="18" x="3" y="3" rx="2"></rect>',
  "tag": '<path d="M12.586 2.586A2 2 0 0 0 11.172 2H4a2 2 0 0 0-2 2v7.172a2 2 0 0 0 .586 1.414l8.704 8.704a2.426 2.426 0 0 0 3.42 0l6.58-6.58a2.426 2.426 0 0 0 0-3.42z"></path> <circle cx="7.5" cy="7.5" r=".5" fill="currentColor"></circle>',
  "terminal": '<path d="M12 19h8"></path> <path d="m4 17 6-6-6-6"></path>',
  "trash-2": '<path d="M10 11v6"></path> <path d="M14 11v6"></path> <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"></path> <path d="M3 6h18"></path> <path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>',
  "triangle-alert": '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"></path> <path d="M12 9v4"></path> <path d="M12 17h.01"></path>',
  "upload": '<path d="M12 3v12"></path> <path d="m17 8-5-5-5 5"></path> <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>',
  "wrench": '<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.106-3.105c.32-.322.863-.22.983.218a6 6 0 0 1-8.259 7.057l-7.91 7.91a1 1 0 0 1-2.999-3l7.91-7.91a6 6 0 0 1 7.057-8.259c.438.12.54.662.219.984z"></path>',
  "x": '<path d="M18 6 6 18"></path> <path d="m6 6 12 12"></path>',
  "zap": '<path d="M15.914 4a1.5 1.5 0 00-2.474-1.561l-9 9A1.5 1.5 0 005.5 14h4.002a.5.5 0 01.471.666L8.086 20a1.5 1.5 0 002.475 1.56l9-9A1.5 1.5 0 0018.5 10h-3.997a.5.5 0 01-.472-.667z"></path>'
};

export const ICON_NAMES = Object.keys(ICON_PATHS);

/** Lucide glyph rendered at the brand's 1.5px stroke, inheriting currentColor. */
export function Icon({ name, size = 16, strokeWidth = 1.5, className = "", style, ...rest }) {
  const d = ICON_PATHS[name];
  if (!d) return null;
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
      className={className}
      style={{ display: "block", flex: "none", ...style }}
      dangerouslySetInnerHTML={{ __html: d }}
      {...rest}
    />
  );
}

// Runs before the stylesheet paints: a stored light/dark choice is on <html> from the first
// frame, so a dark window never flashes the light canvas while React boots. The key and the
// values match APPEARANCE_KEY / ThemeChoice in src/App.tsx.
try {
  var choice = localStorage.getItem("cubes.appearance");
  if (choice === "light" || choice === "dark") document.documentElement.dataset.theme = choice;
} catch (_) {
  /* storage blocked: follow the system */
}

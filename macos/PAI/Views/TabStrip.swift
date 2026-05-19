import SwiftUI

/// Per-pid color, stable across launches. Cheap hash → palette index.
/// Used by the sidebar and the chat bubbles so the same PAI is the same
/// hue everywhere it shows up.
func palette(for pid: Int) -> Color {
    let palette: [Color] = [
        .blue, .green, .orange, .pink, .purple, .teal, .indigo, .mint, .red, .yellow,
    ]
    return palette[abs(pid) % palette.count]
}

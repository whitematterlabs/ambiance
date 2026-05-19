import SwiftUI

/// What the main window's detail pane is showing. The sidebar binds to
/// this; the menubar mutates it before opening the window so a menu click
/// jumps straight to the right view.
enum AppSelection: Hashable {
    case activity
    case procs
    case pai(Int)   // PAI pid
}

@MainActor
final class AppState: ObservableObject {
    @Published var selection: AppSelection? = nil
}

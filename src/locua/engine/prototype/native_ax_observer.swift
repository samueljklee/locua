// Read-only supplemental AX observation. Never requests grants or performs UI actions.
// Usage: native_ax_observer doctor | native_ax_observer observe REQUEST.json
import Foundation
import ApplicationServices
import CryptoKit
import Darwin

let null = NSNull()
let schema = "locua.native_ax_raw.v2"
typealias WindowIDFunction = @convention(c) (AXUIElement, UnsafeMutablePointer<CGWindowID>) -> AXError
let windowIDFunction: WindowIDFunction? = {
    guard let symbol = dlsym(UnsafeMutableRawPointer(bitPattern: -2), "_AXUIElementGetWindow") else { return nil }
    return unsafeBitCast(symbol, to: WindowIDFunction.self)
}()

func nowNS() -> Int64 { Int64(Date().timeIntervalSince1970 * 1_000_000_000) }
func emit(_ value: [String: Any]) throws {
    let data = try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
    FileHandle.standardOutput.write(data); FileHandle.standardOutput.write(Data([10]))
}
func read(_ element: AXUIElement, _ name: String) -> (AXError, CFTypeRef?) {
    var value: CFTypeRef?
    let error = AXUIElementCopyAttributeValue(element, name as CFString, &value)
    return (error, value)
}
func string(_ element: AXUIElement, _ name: String) -> String? {
    let (error, value) = read(element, name)
    guard error == .success, let value, CFGetTypeID(value) == CFStringGetTypeID() else { return nil }
    return value as? String
}
func fact(_ error: AXError, _ value: Any?, _ typeSupported: Bool = true) -> [String: Any] {
    let status = error == .success ? (typeSupported ? "ok" : "unsupported") :
        ([AXError.attributeUnsupported, .noValue].contains(error) ? "unsupported" : "error")
    return ["status": status, "value": status == "ok" ? (value ?? null) : null,
            "ax_error": error.rawValue]
}
func stringFact(_ element: AXUIElement, _ name: String) -> [String: Any] {
    let (error, value) = read(element, name)
    let supported = value.map { CFGetTypeID($0) == CFStringGetTypeID() } ?? false
    return fact(error, supported ? (value as? String) : nil, supported)
}
func boolFact(_ element: AXUIElement, _ name: String) -> [String: Any] {
    let (error, value) = read(element, name)
    let supported = value.map { CFGetTypeID($0) == CFBooleanGetTypeID() } ?? false
    return fact(error, supported ? (value as? Bool) : nil, supported)
}
func settableFact(_ element: AXUIElement, _ name: String) -> [String: Any] {
    var settable: DarwinBoolean = false
    let error = AXUIElementIsAttributeSettable(element, name as CFString, &settable)
    return fact(error, settable.boolValue)
}
func rangeFact(_ element: AXUIElement) -> [String: Any] {
    let (error, value) = read(element, "AXSelectedTextRange")
    guard error == .success, let value, CFGetTypeID(value) == AXValueGetTypeID() else {
        return fact(error, nil, false)
    }
    let ax = unsafeBitCast(value, to: AXValue.self)
    var range = CFRange()
    guard AXValueGetType(ax) == .cfRange, AXValueGetValue(ax, .cfRange, &range) else {
        return fact(error, nil, false)
    }
    return fact(error, ["location": range.location, "length": range.length, "unit": "utf16_code_units"])
}
func frame(_ element: AXUIElement) -> [String: Double]? {
    let (pe, pv) = read(element, "AXPosition"), (se, sv) = read(element, "AXSize")
    guard pe == .success, se == .success, let pv, let sv,
          CFGetTypeID(pv) == AXValueGetTypeID(), CFGetTypeID(sv) == AXValueGetTypeID() else { return nil }
    let p = unsafeBitCast(pv, to: AXValue.self), s = unsafeBitCast(sv, to: AXValue.self)
    var point = CGPoint.zero, size = CGSize.zero
    guard AXValueGetType(p) == .cgPoint, AXValueGetType(s) == .cgSize,
          AXValueGetValue(p, .cgPoint, &point), AXValueGetValue(s, .cgSize, &size),
          point.x.isFinite, point.y.isFinite, size.width.isFinite, size.height.isFinite,
          size.width > 0, size.height > 0 else { return nil }
    return ["x": point.x, "y": point.y, "width": size.width, "height": size.height]
}
func descriptor(_ element: AXUIElement) -> [String: Any] {
    let title = string(element, "AXTitle"), description = string(element, "AXDescription")
    let value = string(element, "AXValue"), identifier = string(element, "AXIdentifier")
    return ["role": string(element, "AXRole") ?? "", "title": title as Any? ?? null,
            "description": description as Any? ?? null, "identifier": identifier as Any? ?? null,
            "label": (title ?? description ?? value ?? identifier) as Any? ?? null]
}
func sameDescriptor(_ observed: [String: Any], _ expected: [String: Any]) -> Bool {
    for field in ["role", "label", "title", "description", "identifier"] {
        if let wanted = expected[field] as? String, observed[field] as? String != wanted { return false }
    }
    return true
}
func matches(_ observed: [String: Any], _ selector: [String: Any]) -> Bool {
    guard sameDescriptor(observed, selector),
          let actualFrame = observed["frame"] as? [String: Double],
          let expectedFrame = selector["frame"] as? [String: Double] else { return false }
    for field in ["x", "y", "width", "height"] {
        guard let a = actualFrame[field], let b = expectedFrame[field], abs(a - b) <= 0.5 else { return false }
    }
    let ancestors = observed["ancestors"] as? [[String: Any]] ?? []
    let wanted = selector["ancestors"] as? [[String: Any]] ?? []
    var start = 0
    for item in wanted {
        guard start < ancestors.count,
              let index = ancestors[start...].firstIndex(where: { sameDescriptor($0, item) }) else { return false }
        start = index + 1
    }
    return true
}
func windowID(_ element: AXUIElement) -> UInt32? {
    guard let fn = windowIDFunction else { return nil }
    var result: CGWindowID = 0
    return fn(element, &result) == .success && result > 0 ? result : nil
}
func currentAncestors(_ element: AXUIElement, _ requestedWindow: UInt32) -> [[String: Any]]? {
    var ancestors: [[String: Any]] = [], seen: [AXUIElement] = [element]
    var current = element
    for _ in 0..<64 {
        let (error, value) = read(current, "AXParent")
        guard error == .success, let value, CFGetTypeID(value) == AXUIElementGetTypeID() else { return nil }
        let parent = unsafeBitCast(value, to: AXUIElement.self)
        if seen.contains(where: { CFEqual($0, parent) }) { return nil }
        seen.append(parent); ancestors.append(descriptor(parent))
        if string(parent, "AXRole") == "AXWindow" {
            return windowID(parent) == requestedWindow ? ancestors : nil
        }
        current = parent
    }
    return nil
}

func editorFacts(_ element: AXUIElement, _ app: AXUIElement) -> [String: Any] {
    let (focusError, focusValue) = read(app, "AXFocusedUIElement")
    var focusMatches: Any = null
    if focusError == .success, let focusValue, CFGetTypeID(focusValue) == AXUIElementGetTypeID() {
        focusMatches = CFEqual(element, focusValue)
    }
    return [
        "raw_value": stringFact(element, "AXValue"), "value_settable": settableFact(element, "AXValue"),
        "selected_text_settable": settableFact(element, "AXSelectedText"),
        "selected_text": stringFact(element, "AXSelectedText"), "selected_range": rangeFact(element),
        "focused_attribute": boolFact(element, "AXFocused"),
        "focused_by_application": fact(focusError, focusMatches is NSNull ? nil : focusMatches, !(focusMatches is NSNull)),
        "enabled": boolFact(element, "AXEnabled")]
}

func observe(_ requestURL: URL) throws -> [String: Any] {
    let data = try Data(contentsOf: requestURL)
    guard let request = try JSONSerialization.jsonObject(with: data) as? [String: Any],
          request["schema"] as? String == "locua.native_ax_request.v1",
          let target = request["target"] as? [String: Any],
          let pid = target["pid"] as? Int32, pid > 0,
          let requestedWindow = target["window_id"] as? UInt32, requestedWindow > 0,
          let selector = request["selector"] as? [String: Any],
          let limits = request["limits"] as? [String: Int],
          let maxNodes = limits["max_nodes"], (1...4000).contains(maxNodes),
          let maxDepth = limits["max_depth"], (1...64).contains(maxDepth),
          let timeoutMS = limits["timeout_ms"], (1...10000).contains(timeoutMS) else {
        throw NSError(domain: "locua.ax", code: 1, userInfo: [NSLocalizedDescriptionKey: "Invalid bounded request"])
    }
    var result: [String: Any] = ["schema": schema, "target": target,
        "snapshot_id": request["snapshot_id"] ?? null, "control_id": request["control_id"] ?? null,
        "selector_sha256": request["selector_sha256"] ?? null,
        "request_sha256": SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined(),
        "started_at_ns": nowNS(), "finished_at_ns": null, "status": "refused", "matches": [],
        "read_only": true, "permission_prompted": false, "provider": "local_macos_ax_supplement"]
    func finish(_ reason: String, _ complete: Bool = false, _ visited: Int = 0) -> [String: Any] {
        result["reason"] = reason; result["finished_at_ns"] = nowNS()
        result["traversal"] = ["complete": complete, "visited": visited]
        return result
    }
    guard AXIsProcessTrusted() else { return finish("accessibility_permission_missing") }
    guard windowIDFunction != nil else { return finish("window_identity_spi_unavailable") }
    let app = AXUIElementCreateApplication(pid)
    AXUIElementSetMessagingTimeout(app, 0.5) // Bound reads on this local AX proxy, no UI mutation.
    let (windowsError, windowValues) = read(app, "AXWindows")
    guard windowsError == .success, let windows = windowValues as? [AXUIElement] else {
        return finish("ax_windows_unreadable")
    }
    let exact = windows.filter { windowID($0) == requestedWindow }
    guard exact.count == 1 else { return finish(exact.isEmpty ? "exact_window_unavailable" : "exact_window_ambiguous") }
    var actualPID: pid_t = 0
    guard AXUIElementGetPid(exact[0], &actualPID) == .success, actualPID == pid else { return finish("window_pid_mismatch") }
    let deadline = DispatchTime.now().uptimeNanoseconds + UInt64(timeoutMS) * 1_000_000
    var stack: [(AXUIElement, [[String: Any]], Int)] = [(exact[0], [], 0)]
    var seen: [AXUIElement] = [], candidates: [(AXUIElement, [String: Any])] = []
    var incompleteReason: String?
    while let (element, ancestors, depth) = stack.popLast() {
        if DispatchTime.now().uptimeNanoseconds >= deadline { incompleteReason = "observation_deadline"; break }
        if seen.contains(where: { CFEqual($0, element) }) { continue }
        if seen.count >= maxNodes { incompleteReason = "node_limit"; break }
        seen.append(element)
        AXUIElementSetMessagingTimeout(element, 0.5)
        var current = descriptor(element)
        current["frame"] = frame(element) as Any? ?? null
        current["ancestors"] = ancestors
        if matches(current, selector) { candidates.append((element, current)) }
        let (error, childValue) = read(element, "AXChildren")
        if error != .success && error != .attributeUnsupported && error != .noValue {
            incompleteReason = "children_unreadable"; break
        }
        if error == .success && childValue != nil && !(childValue is [AXUIElement]) {
            incompleteReason = "children_type_unsupported"; break
        }
        let children = childValue as? [AXUIElement] ?? []
        if depth >= maxDepth && !children.isEmpty { incompleteReason = "depth_limit"; break }
        let parentDescriptor = current.filter { $0.key != "ancestors" && $0.key != "frame" }
        for child in children.reversed() { stack.append((child, [parentDescriptor] + ancestors, depth + 1)) }
    }
    if let reason = incompleteReason { return finish(reason, false, seen.count) }
    guard candidates.count == 1 else { return finish(candidates.isEmpty ? "editor_not_found" : "editor_ambiguous", true, seen.count) }
    let (element, _) = candidates[0]
    var nodePID: pid_t = 0
    guard AXUIElementGetPid(element, &nodePID) == .success, nodePID == pid else { return finish("element_pid_mismatch") }
    guard let liveAncestors = currentAncestors(element, requestedWindow) else { return finish("element_window_ancestry_unproven") }
    var identity = descriptor(element); identity["frame"] = frame(element) as Any? ?? null; identity["ancestors"] = liveAncestors
    guard matches(identity, selector) else { return finish("editor_changed_during_search") }
    guard DispatchTime.now().uptimeNanoseconds < deadline else { return finish("observation_deadline") }
    let facts = editorFacts(element, app)
    let beforeStamp = nowNS()
    // Two complete read sets detect value/selection/focus/writability changes.
    // This is not an atomic AX snapshot and does not exclude an ABA transition.
    let afterFacts = editorFacts(element, app)
    let afterStamp = nowNS()
    guard DispatchTime.now().uptimeNanoseconds < deadline else { return finish("observation_deadline") }
    // Reconcile the exact window and node descriptor again after attribute reads.
    guard windowID(exact[0]) == requestedWindow else { return finish("window_changed_during_read") }
    guard let afterAncestors = currentAncestors(element, requestedWindow) else { return finish("element_window_ancestry_unproven") }
    var after = descriptor(element); after["frame"] = frame(element) as Any? ?? null; after["ancestors"] = afterAncestors
    guard matches(after, selector) else { return finish("editor_changed_during_read") }
    guard NSDictionary(dictionary: identity).isEqual(to: after) else { return finish("editor_identity_changed_during_read") }
    guard NSDictionary(dictionary: facts).isEqual(to: afterFacts) else { return finish("editor_attributes_changed_during_read") }
    result["status"] = "observed"
    result["matches"] = [["identity": identity, "facts": facts]]
    result["stability"] = ["method": "bracketed_attribute_reads",
        "before": ["identity": identity, "facts": facts, "observed_at_ns": beforeStamp],
        "after": ["identity": after, "facts": afterFacts, "observed_at_ns": afterStamp]]
    return finish("unique_semantic_frame_match", true, seen.count)
}

do {
    if CommandLine.arguments.count == 2 && CommandLine.arguments[1] == "doctor" {
        try emit(["schema": "locua.native_ax_doctor.v1", "platform": "macos", "read_only": true,
                  "observation_protocol": schema,
                  "accessibility_trusted": AXIsProcessTrusted(), "permission_prompted": false,
                  "window_identity_spi_available": windowIDFunction != nil,
                  "actions_supported": false, "network_used": false])
    } else if CommandLine.arguments.count == 3 && CommandLine.arguments[1] == "observe" {
        try emit(try observe(URL(fileURLWithPath: CommandLine.arguments[2])))
    } else {
        throw NSError(domain: "locua.ax", code: 2, userInfo: [NSLocalizedDescriptionKey: "Usage: native_ax_observer doctor | observe REQUEST.json"])
    }
} catch {
    try? emit(["schema": schema, "status": "error", "reason": String(describing: error), "read_only": true,
               "permission_prompted": false])
    exit(1)
}

// Local Excel object-model transport. No AppleScript source construction, app
// launch, focus change, permission prompt, global settings or retry-on-write.
import Foundation
import AppKit
import ApplicationServices

struct Refusal: Error { let message: String }
var actionIssued = false
let captureStartedAtNs = UInt64(Date().timeIntervalSince1970 * 1_000_000_000)
func code(_ s: String) -> OSType { s.utf8.reduce(0) { ($0 << 8) | OSType($1) } }
func string(_ object: [String: Any], _ key: String) throws -> String {
    guard let value = object[key] as? String, !value.isEmpty else { throw Refusal(message:"missing_\(key)") }; return value
}
func objectSpec(_ desired: String, _ form: String, _ selector: NSAppleEventDescriptor,
                _ container: NSAppleEventDescriptor = .null()) throws -> NSAppleEventDescriptor {
    let record = NSAppleEventDescriptor.record()
    record.setDescriptor(NSAppleEventDescriptor(typeCode:code(desired)), forKeyword:code("want"))
    record.setDescriptor(NSAppleEventDescriptor(enumCode:code(form)), forKeyword:code("form"))
    record.setDescriptor(selector, forKeyword:code("seld"))
    record.setDescriptor(container, forKeyword:code("from"))
    guard let value = record.coerce(toDescriptorType:code("obj ")) else { throw Refusal(message:"object_descriptor_failed") }
    return value
}
func property(_ name: String, _ object: NSAppleEventDescriptor) throws -> NSAppleEventDescriptor {
    try objectSpec("prop","prop",NSAppleEventDescriptor(typeCode:code(name)),object)
}
func typed(_ value: NSAppleEventDescriptor) -> [String: Any] {
    let type = value.descriptorType
    if ["utxt","utf8","ut16","TEXT","cstr","pstr"].map(code).contains(type), let text = value.stringValue {
        return ["type":"string","value":text]
    }
    if ["bool","true","fals"].map(code).contains(type) { return ["type":"boolean","value":value.booleanValue] }
    if ["long","shor","comp","doub","sing"].map(code).contains(type), value.doubleValue.isFinite {
        return ["type":"number","value":value.doubleValue]
    }
    if type == code("null") || (type == code("type") && value.typeCodeValue == code("msng")) {
        return ["type":"blank","value":NSNull()]
    }
    return ["type":"unknown","value":NSNull(),"descriptor_type":type]
}
func equal(_ a: Any, _ b: Any) -> Bool {
    guard let a = try? JSONSerialization.data(withJSONObject:a,options:[.sortedKeys,.fragmentsAllowed]),
          let b = try? JSONSerialization.data(withJSONObject:b,options:[.sortedKeys,.fragmentsAllowed]) else { return false }
    return a == b
}
func coordinate(_ value: String) throws -> (Int,Int) {
    let regex = try NSRegularExpression(pattern:"^([A-Z]{1,3})([1-9][0-9]{0,6})$")
    guard let match=regex.firstMatch(in:value,range:NSRange(value.startIndex...,in:value)),
          let letters=Range(match.range(at:1),in:value), let digits=Range(match.range(at:2),in:value),
          let row=Int(value[digits]) else { throw Refusal(message:"invalid_A1_address") }
    let column=value[letters].utf8.reduce(0){$0*26+Int($1)-64}
    guard row <= 1048576 && column <= 16384 else { throw Refusal(message:"address_out_of_bounds") }
    return (row,column)
}
func address(_ row:Int,_ column:Int) -> String {
    var column=column; var text=""
    while column>0 { column-=1; text=String(UnicodeScalar(65+column%26)!)+text; column/=26 }
    return text+String(row)
}
func cells(_ range:String) throws -> [String] {
    let parts=range.split(separator:":",omittingEmptySubsequences:false).map(String.init)
    guard parts.count == 1 || parts.count == 2 else { throw Refusal(message:"invalid_range") }
    let (r1,c1)=try coordinate(parts[0]); let(r2,c2)=try coordinate(parts.last!)
    guard r2>=r1 && c2>=c1 && (r2-r1+1)*(c2-c1+1)<=64 else { throw Refusal(message:"range_exceeds_64_cells_or_inverted") }
    return (r1...r2).flatMap{r in (c1...c2).map{address(r,$0)}}
}
final class Excel {
    let target:NSAppleEventDescriptor; let pid:pid_t; let executable:String
    let deadline=Date().addingTimeInterval(10)
    init(_ request:[String:Any]) throws {
        guard let value=request["pid"] as? Int, value>0 && value<Int(Int32.max) else {throw Refusal(message:"invalid_pid")}
        pid=pid_t(value); executable=try string(request,"executable")
        target=NSAppleEventDescriptor(processIdentifier:pid)
        try identity()
    }
    func identity() throws {
        guard let app=NSRunningApplication(processIdentifier:pid), app.bundleIdentifier=="com.microsoft.Excel",
              !app.isTerminated,app.executableURL?.standardizedFileURL.path==executable else {throw Refusal(message:"Excel_PID_executable_identity_unproved")}
    }
    func permission() throws -> OSStatus {
        try identity()
        return AEDeterminePermissionToAutomateTarget(target.aeDesc,code("****"),code("****"),false)
    }
    func send(_ eventClass:String,_ id:String,_ object:NSAppleEventDescriptor,_ extra:[String:NSAppleEventDescriptor]=[:]) throws -> NSAppleEventDescriptor {
        try identity()
        guard AEDeterminePermissionToAutomateTarget(target.aeDesc,code("****"),code("****"),false) == noErr else {throw Refusal(message:"Automation_permission_missing_no_prompt")}
        guard Date()<deadline else {throw Refusal(message:"Apple_event_budget_expired")}
        let event=NSAppleEventDescriptor(eventClass:code(eventClass),eventID:code(id),targetDescriptor:target,returnID:AEReturnID(kAutoGenerateReturnID),transactionID:AETransactionID(kAnyTransactionID))
        event.setParam(object,forKeyword:code("----"))
        for (key,value) in extra {event.setParam(value,forKeyword:code(key))}
        let reply=try event.sendEvent(options:[.waitForReply,.neverInteract],timeout:min(2,deadline.timeIntervalSinceNow))
        if let number=reply.paramDescriptor(forKeyword:code("errn")),number.int32Value != 0 {throw Refusal(message:"Excel_Apple_event_error_\(number.int32Value)")}
        try identity()
        return reply.paramDescriptor(forKeyword:code("----")) ?? .null()
    }
    func get(_ propertyCode:String,_ object:NSAppleEventDescriptor) throws -> NSAppleEventDescriptor {try send("core","getd",property(propertyCode,object))}
    func observe(_ cell:NSAppleEventDescriptor,_ expectedAddress:String) throws -> [String:Any] {
        let actual=try send("sTBL","1515",cell,["5121":NSAppleEventDescriptor(boolean:false),"5122":NSAppleEventDescriptor(boolean:false),"5123":NSAppleEventDescriptor(boolean:false)])
        guard actual.stringValue==expectedAddress else {throw Refusal(message:"range_address_did_not_match")}
        let merged=typed(try get("1588",cell)); let array=typed(try get("1572",cell))
        let value=typed(try get("DPV2",cell)); let formula=typed(try get("1562",cell)); let hasFormula=typed(try get("1573",cell))
        let recheck=typed(try get("DPV2",cell)); let formulaRecheck=typed(try get("1562",cell))
        return ["address":expectedAddress,"value2":value,"formula":formula,"has_formula":hasFormula,
                "merged":merged,"array_formula_member":array,
                "value_stable":equal(value,recheck) && equal(formula,formulaRecheck),
                "plane":"committed_document","editor_buffer_proven":false,"focus_proven":false,"saved_file_proven":false]
    }
}
func run(_ request:[String:Any]) throws -> [String:Any] {
    let operation=try string(request,"operation")
    guard ["doctor","observe","set_cell","save"].contains(operation) else {throw Refusal(message:"unsupported_operation")}
    let excel=try Excel(request);let status=try excel.permission()
    if operation=="doctor" || status != noErr {return ["schema":"locua.excel.events.v1","status":status == noErr ? "permission_ready":"permission_missing","automation_os_status":status,"permission_prompted":false,"pid":excel.pid]}
    let name=try string(request,"workbook_name");let fullName=try string(request,"workbook_full_name");let sheetName=try string(request,"sheet")
    let workbook=try objectSpec("X141","name",NSAppleEventDescriptor(string:name))
    guard try excel.get("1773",workbook).stringValue==fullName else {throw Refusal(message:"workbook_full_name_did_not_match")}
    let sheet=try objectSpec("XwSH","name",NSAppleEventDescriptor(string:sheetName),workbook)
    guard try excel.get("pnam",sheet).stringValue==sheetName else {throw Refusal(message:"worksheet_identity_did_not_match")}
    let range=try string(request,"range");let addresses=try cells(range)
    var outputCells:[[String:Any]]=[]
    let readonly=typed(try excel.get("1830",workbook));let protected=typed(try excel.get("1656",sheet))
    var actionStarted=false
    for a in addresses {
        let cell=try objectSpec("X117","name",NSAppleEventDescriptor(string:a),sheet)
        let before=try excel.observe(cell,a)
        if operation=="set_cell" {
            guard addresses.count==1,request["authorize_write"] as? Bool == true,
                  equal(readonly,["type":"boolean","value":false]),equal(protected,["type":"boolean","value":false]),
                  before["value_stable"] as? Bool == true,
                  equal(before["merged"]!,["type":"boolean","value":false]),
                  equal(before["array_formula_member"]!,["type":"boolean","value":false]),
                  let expected=request["expected_before"] as? [String:Any],
                  ["value2","formula","has_formula"].allSatisfy({equal(before[$0]!,expected[$0] ?? NSNull())}),
                  let desired=request["desired"] as? [String:Any],let kind=desired["type"] as? String else {throw Refusal(message:"write_preconditions_unproved")}
            let value:NSAppleEventDescriptor;let propertyCode:String
            switch kind {
            case "formula": guard let text=desired["value"] as? String,text.hasPrefix("=") else {throw Refusal(message:"invalid_formula")};value=NSAppleEventDescriptor(string:text);propertyCode="1562"
            case "string": guard let text=desired["value"] as? String,!text.hasPrefix("=") else {throw Refusal(message:"formula_like_literal_requires_separate_supported_route")};value=NSAppleEventDescriptor(string:text);propertyCode="DPV2"
            case "number": guard let number=desired["value"] as? NSNumber,CFGetTypeID(number) != CFBooleanGetTypeID(),number.doubleValue.isFinite else {throw Refusal(message:"invalid_number")};value=NSAppleEventDescriptor(double:number.doubleValue);propertyCode="DPV2"
            case "boolean": guard let number=desired["value"] as? NSNumber,CFGetTypeID(number)==CFBooleanGetTypeID() else {throw Refusal(message:"invalid_boolean")};value=NSAppleEventDescriptor(boolean:number.boolValue);propertyCode="DPV2"
            default: throw Refusal(message:"unsupported_cell_literal")
            }
            // A timeout/error after this point is unknown effect. No automatic retry.
            actionStarted=true
            actionIssued=true
            do {_ = try excel.send("core","setd",property(propertyCode,cell),["data":value]);outputCells.append(try excel.observe(cell,a))}
            catch {return ["schema":"locua.excel.events.v1","status":"effect_unknown","action_started":true,"reason":String(describing:error)]}
        } else {outputCells.append(before)}
    }
    if operation=="save" {
        guard request["authorize_save_workbook"] as? Bool == true,equal(readonly,["type":"boolean","value":false]) else {throw Refusal(message:"save_authorization_or_writability_unproved")}
        actionStarted=true
        actionIssued=true
        do {_ = try excel.send("core","save",workbook)}
        catch {return ["schema":"locua.excel.events.v1","status":"effect_unknown","action_started":true,"reason":String(describing:error)]}
    }
    guard try excel.get("1773",workbook).stringValue==fullName else {return ["schema":"locua.excel.events.v1","status":"effect_unknown","action_started":actionStarted,"reason":"workbook_identity_changed"]}
    return ["schema":"locua.excel.events.v1","status":"observed","operation":operation,
            "pid":excel.pid,"executable":excel.executable,"workbook_name":name,"workbook_full_name":fullName,"sheet":sheetName,"range":range,
            "cells":outputCells,"workbook_read_only":readonly,"sheet_protected":protected,"workbook_saved_flag":typed(try excel.get("1842",workbook)),
            "action_started":actionStarted,"atomic_capture":false,"saved_file_proven":false,"permission_prompted":false]
}
if CommandLine.arguments.dropFirst().elementsEqual(["--self-test"]) {
    precondition(equal(typed(NSAppleEventDescriptor(string:"  exact\n")),["type":"string","value":"  exact\n"]))
    precondition(equal(typed(NSAppleEventDescriptor(string:"")),["type":"string","value":""]))
    precondition(equal(typed(NSAppleEventDescriptor(boolean:false)),["type":"boolean","value":false]))
    precondition(equal(typed(NSAppleEventDescriptor(int32:0)),["type":"number","value":0]))
    precondition(!equal(["type":"boolean","value":true],["type":"number","value":1]))
    precondition(try! cells("B2:C3") == ["B2","C2","B3","C3"])
    precondition((try? cells("A1:C99")) == nil)
    let specimen=try! objectSpec("X117","name",NSAppleEventDescriptor(string:"B2"))
    precondition(specimen.descriptorType==code("obj "))
    print("{\"status\":\"pass\",\"pure_descriptor_checks\":8,\"apple_events_sent\":0,\"permission_queries\":0}")
    exit(0)
}
var output:[String:Any]
do {
    let input=FileHandle.standardInput.readDataToEndOfFile()
    guard input.count<=1_048_576,let request=try JSONSerialization.jsonObject(with:input) as? [String:Any] else {throw Refusal(message:"invalid_bounded_request")}
    output=try run(request)
} catch {output=["schema":"locua.excel.events.v1","status":actionIssued ? "effect_unknown":"refused","action_started":actionIssued,"reason":String(describing:error),"permission_prompted":false]}
output["capture_started_at_ns"] = captureStartedAtNs
output["capture_finished_at_ns"] = UInt64(Date().timeIntervalSince1970 * 1_000_000_000)
let data=try JSONSerialization.data(withJSONObject:output,options:[.sortedKeys])
FileHandle.standardOutput.write(data);FileHandle.standardOutput.write(Data([10]))

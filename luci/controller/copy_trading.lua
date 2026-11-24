module("luci.controller.copy_trading", package.seeall)

local http = require "luci.http"
local fs = require "nixio.fs"
local sys = require "luci.sys"
local disp = require "luci.dispatcher"

local BASE = "/root/ct-instances"
local HELPER = "/opt/ct-app/manager.py"

local function run(cmd)
	return sys.exec(cmd) or ""
end

local function trim(value)
	return (tostring(value or ""):gsub("^%s+", ""):gsub("%s+$", ""))
end

local function valid_instance(name)
	return type(name) == "string" and name:match("^[%w_%-]+$") ~= nil
		and fs.stat(BASE .. "/" .. name .. "/instance.json") ~= nil
end

local function esc(value)
	return (tostring(value or ""):gsub("&", "&amp;"):gsub("<", "&lt;"):gsub(">", "&gt;"))
end

local function redirect_back(message)
	local url = disp.build_url("admin", "services", "copy_trading")
	if message and message ~= "" then
		local encoded = message:gsub("([^%w%-%._~])", function(ch)
			return string.format("%%%02X", string.byte(ch))
		end)
		url = url .. "?msg=" .. encoded
	end
	http.redirect(url)
end

function index()
	entry({"admin", "services", "copy_trading"}, call("action"), _("Copy Trading"), 80)
end

function action()
	local act = trim(http.formvalue("do") or "")
	local name = trim(http.formvalue("id") or "")
	local msg = ""

	if act == "create" then
		local new_name = trim(http.formvalue("name") or "")
		local port = trim(http.formvalue("port") or "")
		if not new_name:match("^[%w_%-]+$") then
			redirect_back("实例名只能包含字母、数字、下划线、短横线")
			return
		end
		if port ~= "" and not port:match("^%d+$") then
			redirect_back("端口必须是数字")
			return
		end
		local cmd = "python3 " .. HELPER .. " create " .. new_name
		if port ~= "" then cmd = cmd .. " " .. port end
		redirect_back(trim(run(cmd .. " 2>&1")))
		return
	end

	if act == "ensure" then
		if valid_instance(name) then
			redirect_back(trim(run("python3 " .. HELPER .. " ensure " .. name .. " 2>&1")))
		else
			redirect_back("实例不存在")
		end
		return
	end

	if act == "stop_all_engines" then
		redirect_back(trim(run("python3 " .. HELPER .. " stop_all 2>&1")))
		return
	end

	if act == "restore" then
		local archive = trim(http.formvalue("archive") or "")
		if archive:match("^[%w%._%-]+$") then
			redirect_back(trim(run("python3 " .. HELPER .. " restore " .. archive .. " 2>&1")))
		else
			redirect_back("归档名不合法")
		end
		return
	end

	if act == "delete" then
		if valid_instance(name) then
			redirect_back(trim(run("python3 " .. HELPER .. " delete " .. name .. " 2>&1")))
		else
			redirect_back("实例不存在")
		end
		return
	end

	if act:match("^start_") or act:match("^stop_") or act:match("^restart_") then
		if act:match("^start_") and valid_instance(name) then
			redirect_back(trim(run("python3 " .. HELPER .. " " .. act .. " " .. name .. " 2>&1")))
		elseif act:match("^stop_") and valid_instance(name) then
			redirect_back(trim(run("python3 " .. HELPER .. " " .. act .. " " .. name .. " 2>&1")))
		elseif act:match("^restart_") and valid_instance(name) then
			redirect_back(trim(run("python3 " .. HELPER .. " " .. act .. " " .. name .. " 2>&1")))
		else
			redirect_back("实例不存在")
		end
		return
	end

	local log_instance = trim(http.formvalue("log_instance") or "")
	if not valid_instance(log_instance) then
		local first = trim(run("python3 " .. HELPER .. " list 2>/dev/null"))
		log_instance = first:match("^[^\r\n]+") or ""
	end
	local log_type = trim(http.formvalue("log_type") or "copy")
	if log_type ~= "web" and log_type ~= "strategy" then log_type = "copy" end
	local log = ""
	if valid_instance(log_instance) then
		log = run("tail -n 120 " .. BASE .. "/" .. log_instance .. "/" .. log_type .. ".log 2>&1")
	end

	local host = http.getenv("HTTP_HOST") or http.getenv("SERVER_NAME") or http.getenv("SERVER_ADDR") or "127.0.0.1"
	host = host:gsub(":%d+$", "")
	local table_html = run("python3 " .. HELPER .. " dashboard " .. host .. " 2>&1")
	local trash_html = run("python3 " .. HELPER .. " trash 2>&1")
	local message = trim(http.formvalue("msg") or "")
	local notice = ""
	if message ~= "" then
		notice = '<div class="alert-message">' .. esc(message) .. '</div>'
	end
	local combined = notice .. '<div style="width:100%;overflow-x:auto">' .. table_html .. '</div>'
		.. '<h3 style="margin-top:1.2em">已删除实例（trash）</h3>' .. trash_html
		.. '<p class="cbi-section-descr">删除不会物理清除；可从 trash 恢复目录和三个服务。</p>'
	local next_port = trim(run("python3 " .. HELPER .. " list 2>/dev/null"))

	luci.template.render("copy_trading5", {
		table_html = combined,
		log = log,
		log_instance = log_instance,
		log_type = log_type,
	})
end

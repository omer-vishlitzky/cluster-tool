# test_cluster_tool.py
import argparse
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

loader = importlib.machinery.SourceFileLoader("cluster_tool", "./cluster-tool")
spec = importlib.util.spec_from_loader("cluster_tool", loader, origin="./cluster-tool")
ct = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ct)


class TestStateManagement(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.state_file = os.path.join(self.tmpdir, "state.json")
        ct.LOCAL_STATE_FILE = type(ct.LOCAL_STATE_FILE)(self.state_file)
        ct.LOCAL_STATE_DIR = type(ct.LOCAL_STATE_DIR)(self.tmpdir)

    def test_load_empty_state(self):
        state = ct.load_state()
        self.assertIsNone(state["snapshot"])
        self.assertEqual(state["clones"], {})
        self.assertEqual(state["next_subnet"], 160)

    def test_save_and_load_roundtrip(self):
        state = {"snapshot": {"source": "abc"}, "clones": {}, "next_subnet": 162}
        ct.save_state(state)
        loaded = ct.load_state()
        self.assertEqual(loaded, state)

    def test_allocate_subnet_increments(self):
        state = ct.load_state()
        s1 = ct.allocate_subnet(state)
        s2 = ct.allocate_subnet(state)
        self.assertEqual(s1, 160)
        self.assertEqual(s2, 161)
        self.assertEqual(state["next_subnet"], 162)


class TestIDGeneration(unittest.TestCase):
    def test_clone_id_format(self):
        cid = ct.generate_clone_id()
        self.assertEqual(len(cid), 8)
        int(cid, 16)  # must be valid hex

    def test_clone_ids_are_unique(self):
        ids = {ct.generate_clone_id() for _ in range(100)}
        self.assertGreater(len(ids), 90)

    def test_mac_format(self):
        mac = ct.generate_mac()
        parts = mac.split(":")
        self.assertEqual(len(parts), 6)
        self.assertEqual(parts[0], "02")
        self.assertEqual(parts[1], "00")
        self.assertEqual(parts[2], "00")


class TestTemplates(unittest.TestCase):
    def test_primary_network_xml(self):
        xml = ct.gen_primary_network_xml("a1b2c3d4", 160, "02:00:00:aa:bb:cc")
        self.assertIn("<name>test-infra-net-a1b2c3d4</name>", xml)
        self.assertIn("192.168.160.1", xml)
        self.assertIn("192.168.160.10", xml)
        self.assertIn("02:00:00:aa:bb:cc", xml)
        self.assertIn("api.test-infra-cluster-a1b2c3d4.redhat.com", xml)
        self.assertIn("192.168.160.10", xml)
        self.assertIn("192.168.160.10", xml)

    def test_secondary_network_xml(self):
        xml = ct.gen_secondary_network_xml("a1b2c3d4", 160, 178, "02:00:00:dd:ee:ff")
        self.assertIn("<name>test-infra-secondary-network-a1b2c3d4</name>", xml)
        self.assertIn("192.168.178.1", xml)
        self.assertIn("192.168.178.10", xml)
        self.assertIn("192.168.160.10", xml)  # DNS points to primary VIP

    def test_vm_xml(self):
        xml = ct.gen_vm_xml("a1b2c3d4", "/path/overlay.qcow2", "02:00:00:aa:bb:cc", "02:00:00:dd:ee:ff")
        self.assertIn("<name>test-infra-cluster-a1b2c3d4-master-0</name>", xml)
        self.assertIn("/path/overlay.qcow2", xml)
        self.assertIn("02:00:00:aa:bb:cc", xml)
        self.assertIn("02:00:00:dd:ee:ff", xml)
        self.assertIn("test-infra-net-a1b2c3d4", xml)
        self.assertIn("test-infra-secondary-network-a1b2c3d4", xml)
        self.assertIn(str(ct.VM_MEMORY_KIB), xml)

    def test_haproxy_additions(self):
        use_backends, backends = ct.gen_haproxy_additions("a1b2c3d4", 160)
        self.assertIn("api-a1b2c3d4", use_backends["api"])
        self.assertIn("req_ssl_sni", use_backends["api"])
        self.assertIn("hdr_end(host)", use_backends["ingress-http"])
        self.assertIn("192.168.160.10:6443", backends)
        self.assertIn("192.168.160.10:443", backends)
        self.assertIn("192.168.160.10:80", backends)

    def test_dnsmasq_conf(self):
        conf = ct.gen_dnsmasq_conf("a1b2c3d4")
        self.assertEqual(conf, "address=/test-infra-cluster-a1b2c3d4.redhat.com/10.1.155.16\n")

    def test_dnsmasq_conf_resolves_all_subdomains(self):
        conf = ct.gen_dnsmasq_conf("mytest")
        self.assertIn("address=/test-infra-cluster-mytest.redhat.com/", conf)
        self.assertNotIn("api.", conf)
        self.assertNotIn("apps.", conf)

    def test_dnsmasq_conf_uses_baremetal_ip(self):
        conf = ct.gen_dnsmasq_conf("xyz")
        self.assertIn(ct.BAREMETAL_IP, conf)

    def test_dnsmasq_conf_different_ids_produce_different_configs(self):
        conf1 = ct.gen_dnsmasq_conf("aaaa")
        conf2 = ct.gen_dnsmasq_conf("bbbb")
        self.assertNotEqual(conf1, conf2)
        self.assertIn("aaaa", conf1)
        self.assertIn("bbbb", conf2)


class TestDnsEntry(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_dnsmasq_dir = ct.DNSMASQ_DIR
        ct.DNSMASQ_DIR = Path(self.tmpdir)

    def tearDown(self):
        ct.DNSMASQ_DIR = self._orig_dnsmasq_dir

    @patch("subprocess.run")
    def test_add_dns_entry_creates_file(self, mock_run):
        ct.add_dns_entry("test01")
        conf = Path(self.tmpdir) / "cluster-test01.conf"
        self.assertTrue(conf.exists())
        self.assertIn("test-infra-cluster-test01.redhat.com", conf.read_text())

    @patch("subprocess.run")
    def test_add_dns_entry_reloads_nm(self, mock_run):
        ct.add_dns_entry("test01")
        mock_run.assert_called_once_with(["nmcli", "general", "reload"], check=True)

    @patch("subprocess.run")
    def test_add_dns_entry_is_idempotent(self, mock_run):
        ct.add_dns_entry("test01")
        content1 = (Path(self.tmpdir) / "cluster-test01.conf").read_text()
        ct.add_dns_entry("test01")
        content2 = (Path(self.tmpdir) / "cluster-test01.conf").read_text()
        self.assertEqual(content1, content2)

    @patch("subprocess.run")
    def test_remove_dns_entry_deletes_file(self, mock_run):
        conf = Path(self.tmpdir) / "cluster-test01.conf"
        conf.write_text("address=/test.redhat.com/10.1.155.16\n")
        ct.remove_dns_entry("test01")
        self.assertFalse(conf.exists())

    @patch("subprocess.run")
    def test_remove_dns_entry_reloads_nm(self, mock_run):
        conf = Path(self.tmpdir) / "cluster-test01.conf"
        conf.write_text("test")
        ct.remove_dns_entry("test01")
        mock_run.assert_called_once_with(["nmcli", "general", "reload"], check=False)

    @patch("subprocess.run")
    def test_remove_dns_entry_missing_file_no_error(self, mock_run):
        ct.remove_dns_entry("nonexistent")

    @patch("subprocess.run")
    def test_multiple_clones_separate_files(self, mock_run):
        ct.add_dns_entry("clone1")
        ct.add_dns_entry("clone2")
        self.assertTrue((Path(self.tmpdir) / "cluster-clone1.conf").exists())
        self.assertTrue((Path(self.tmpdir) / "cluster-clone2.conf").exists())
        ct.remove_dns_entry("clone1")
        self.assertFalse((Path(self.tmpdir) / "cluster-clone1.conf").exists())
        self.assertTrue((Path(self.tmpdir) / "cluster-clone2.conf").exists())


class TestTransactionalBoot(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        ct.LOCAL_STATE_FILE = Path(os.path.join(self.tmpdir, "state.json"))
        ct.LOCAL_STATE_DIR = Path(self.tmpdir)
        ct.save_state({
            "snapshot": {
                "source_cluster": "6ef80144",
                "source_disk": "/fake/disk",
                "golden_snapshot": "/root/.cluster-tool/golden-snapshot.qcow2",
                "created_at": "2026-01-01T00:00:00Z",
            },
            "clones": {},
            "next_subnet": 160,
        })
        self.calls = []

    def _boot_args(self):
        return argparse.Namespace(name="aabbccdd")

    def _make_ssh_mock(self, fail_on):
        def ssh(cmd, check=True):
            self.calls.append((cmd, check))
            if fail_on in cmd and check:
                raise SystemExit(f"Simulated: {fail_on}")
            r = MagicMock()
            r.returncode = 0
            r.stdout = "fake-ingress-cn" if "ingress-cn" in cmd else ""
            r.stderr = ""
            return r
        return ssh

    def _cleanup_cmds(self):
        return [cmd for cmd, chk in self.calls if not chk and any(
            k in cmd for k in ["rm -f /data/cluster-tool/overlays",
                                "virsh net-destroy", "virsh net-undefine",
                                "virsh destroy ", "virsh undefine "]
        )]

    @patch("time.sleep")
    @patch.object(ct, "write_remote_file")
    @patch.object(ct, "add_dns_entry")
    @patch.object(ct, "scp_from_baremetal")
    def test_failure_at_network_cleans_overlay(self, *_):
        with patch.object(ct, "ssh_baremetal", side_effect=self._make_ssh_mock("virsh net-define /tmp/net-")):
            with self.assertRaises(SystemExit):
                ct.cmd_boot(self._boot_args())

        cleanup = self._cleanup_cmds()
        self.assertTrue(any("rm -f" in c and "overlays/aabbccdd" in c for c in cleanup))
        self.assertFalse(any("virsh destroy" in c for c in cleanup))
        self.assertEqual(ct.load_state()["clones"], {})

    @patch("time.sleep")
    @patch.object(ct, "write_remote_file")
    @patch.object(ct, "add_dns_entry")
    @patch.object(ct, "scp_from_baremetal")
    def test_failure_at_vm_cleans_overlay_and_networks(self, *_):
        with patch.object(ct, "ssh_baremetal", side_effect=self._make_ssh_mock("virsh define /tmp/vm-")):
            with self.assertRaises(SystemExit):
                ct.cmd_boot(self._boot_args())

        cleanup = self._cleanup_cmds()
        self.assertTrue(any("rm -f" in c and "overlays/aabbccdd" in c for c in cleanup))
        self.assertTrue(any("net-destroy test-infra-net-aabbccdd" in c for c in cleanup))
        self.assertTrue(any("net-destroy test-infra-secondary-network-aabbccdd" in c for c in cleanup))
        self.assertFalse(any("virsh destroy" in c for c in cleanup))
        self.assertEqual(ct.load_state()["clones"], {})

    @patch("time.sleep")
    @patch.object(ct, "write_remote_file")
    @patch.object(ct, "add_dns_entry")
    @patch.object(ct, "scp_from_baremetal")
    def test_failure_at_recert_cleans_everything(self, *_):
        with patch.object(ct, "ssh_baremetal", side_effect=self._make_ssh_mock("podman run --rm --name recert")):
            with self.assertRaises(SystemExit):
                ct.cmd_boot(self._boot_args())

        cleanup = self._cleanup_cmds()
        self.assertTrue(any("rm -f" in c and "overlays/aabbccdd" in c for c in cleanup))
        self.assertTrue(any("net-destroy test-infra-net-aabbccdd" in c for c in cleanup))
        self.assertTrue(any("net-destroy test-infra-secondary-network-aabbccdd" in c for c in cleanup))
        self.assertTrue(any("virsh destroy" in c for c in cleanup))
        self.assertEqual(ct.load_state()["clones"], {})

    @patch("time.sleep")
    @patch.object(ct, "write_remote_file")
    @patch.object(ct, "add_dns_entry")
    @patch.object(ct, "scp_from_baremetal")
    def test_cleanup_runs_in_reverse_order(self, *_):
        with patch.object(ct, "ssh_baremetal", side_effect=self._make_ssh_mock("podman run --rm --name recert")):
            with self.assertRaises(SystemExit):
                ct.cmd_boot(self._boot_args())

        cleanup = self._cleanup_cmds()
        vm_destroy_idx = next((i for i, c in enumerate(cleanup) if "virsh destroy " in c), -1)
        vm_undefine_idx = next((i for i, c in enumerate(cleanup) if "virsh undefine " in c), -1)
        sec_net_destroy_idx = next((i for i, c in enumerate(cleanup) if "net-destroy test-infra-secondary" in c), -1)
        pri_net_destroy_idx = next((i for i, c in enumerate(cleanup) if "net-destroy test-infra-net-" in c), -1)
        overlay_idx = next((i for i, c in enumerate(cleanup) if "rm -f" in c), -1)
        self.assertLess(vm_destroy_idx, vm_undefine_idx)
        self.assertLess(vm_undefine_idx, sec_net_destroy_idx)
        self.assertLess(sec_net_destroy_idx, pri_net_destroy_idx)
        self.assertLess(pri_net_destroy_idx, overlay_idx)


    _MOCK_CO_JSON = json.dumps({"items": [
        {"metadata": {"name": "test"}, "status": {"conditions": [
            {"type": "Available", "status": "True"},
            {"type": "Progressing", "status": "False"},
            {"type": "Degraded", "status": "False"},
        ]}}
    ]})
    _MOCK_NODES_JSON = json.dumps({"items": [
        {"metadata": {"name": "test-node"}, "status": {"conditions": [
            {"type": "Ready", "status": "True"},
        ]}}
    ]})

    def _make_all_succeed_ssh(self):
        mock_co = self._MOCK_CO_JSON
        mock_nodes = self._MOCK_NODES_JSON
        def ssh(cmd, check=True):
            self.calls.append((cmd, check))
            r = MagicMock()
            r.returncode = 0
            if "ingress-cn" in cmd:
                r.stdout = "fake-ingress-cn"
            elif "infrastructure cluster" in cmd:
                r.stdout = "https://api.test-infra-cluster-aabbccdd.redhat.com:6443"
            elif "get co -o json" in cmd:
                r.stdout = mock_co
            elif "get nodes -o json" in cmd:
                r.stdout = mock_nodes
            else:
                r.stdout = "ok"
            r.stderr = ""
            return r
        return ssh

    @patch("time.sleep")
    @patch.object(ct, "write_remote_file")
    @patch.object(ct, "remove_dns_entry")
    def test_failure_at_hosts_rolls_back_haproxy(self, *_):
        haproxy_removed = []

        def track_remove_haproxy(clone_id):
            haproxy_removed.append(clone_id)

        with patch.object(ct, "ssh_baremetal", side_effect=self._make_all_succeed_ssh()), \
             patch.object(ct, "add_dns_entry", side_effect=subprocess.CalledProcessError(1, "sudo")), \
             patch.object(ct, "remove_haproxy_clone", side_effect=track_remove_haproxy):
            with self.assertRaises(SystemExit):
                ct.cmd_boot(self._boot_args())

        self.assertEqual(haproxy_removed, ["aabbccdd"])
        self.assertEqual(ct.load_state()["clones"], {})
        kubeconfig = ct.KUBECONFIG_DIR / "aabbccdd.kubeconfig"
        self.assertFalse(kubeconfig.exists())

    @patch("time.sleep")
    @patch.object(ct, "write_remote_file")
    @patch.object(ct, "add_dns_entry")
    def test_called_process_error_triggers_rollback(self, *_):
        def ssh_fail_at_haproxy(cmd, check=True):
            self.calls.append((cmd, check))
            if "cat /etc/haproxy" in cmd and check:
                raise subprocess.CalledProcessError(1, cmd, stderr="connection refused")
            r = MagicMock()
            r.returncode = 0
            r.stdout = "ok"
            r.stderr = ""
            return r

        with patch.object(ct, "ssh_baremetal", side_effect=ssh_fail_at_haproxy):
            with self.assertRaises(SystemExit):
                ct.cmd_boot(self._boot_args())

        cleanup = self._cleanup_cmds()
        self.assertTrue(any("virsh destroy " in c for c in cleanup))
        self.assertTrue(any("net-destroy" in c for c in cleanup))
        self.assertTrue(any("rm -f" in c for c in cleanup))
        self.assertEqual(ct.load_state()["clones"], {})

    @patch("time.sleep")
    @patch.object(ct, "write_remote_file")
    @patch.object(ct, "remove_dns_entry")
    @patch.object(ct, "remove_haproxy_clone")
    @patch.object(ct, "add_dns_entry")
    def test_success_saves_state(self, *_):
        with patch.object(ct, "ssh_baremetal", side_effect=self._make_all_succeed_ssh()):
            ct.cmd_boot(self._boot_args())

        state = ct.load_state()
        self.assertIn("aabbccdd", state["clones"])
        self.assertEqual(state["clones"]["aabbccdd"]["subnet_primary"], 160)
        kubeconfig = ct.KUBECONFIG_DIR / "aabbccdd.kubeconfig"
        self.assertTrue(kubeconfig.exists())
        kubeconfig.unlink(missing_ok=True)

    @patch("time.sleep")
    @patch.object(ct, "write_remote_file")
    @patch.object(ct, "remove_dns_entry")
    @patch.object(ct, "remove_haproxy_clone")
    @patch.object(ct, "add_dns_entry")
    def test_recert_uses_key_preservation(self, *_):
        with patch.object(ct, "ssh_baremetal", side_effect=self._make_all_succeed_ssh()):
            ct.cmd_boot(self._boot_args())

        recert_cmd = next(cmd for cmd, _ in self.calls if "--use-key" in cmd)
        self.assertIn("--use-key kube-apiserver-lb-signer:/tmp/crypto/lb-signer.key", recert_cmd)
        self.assertIn("--use-key kube-apiserver-localhost-signer:/tmp/crypto/localhost-signer.key", recert_cmd)
        self.assertIn("--use-key kube-apiserver-service-network-signer:/tmp/crypto/service-network-signer.key", recert_cmd)
        self.assertIn("--use-key fake-ingress-cn:/tmp/crypto/ingress.key", recert_cmd)
        self.assertIn("--cn-san-replace api.test-infra-cluster-6ef80144.redhat.com:api.test-infra-cluster-aabbccdd.redhat.com", recert_cmd)
        self.assertIn("--cn-san-replace api-int.test-infra-cluster-6ef80144.redhat.com:api-int.test-infra-cluster-aabbccdd.redhat.com", recert_cmd)
        self.assertIn("--cn-san-replace *.apps.test-infra-cluster-6ef80144.redhat.com:*.apps.test-infra-cluster-aabbccdd.redhat.com", recert_cmd)
        self.assertIn("--cn-san-replace test-infra-cluster-6ef80144-master-0:test-infra-cluster-aabbccdd-master-0", recert_cmd)
        self.assertIn("--cn-san-replace system:node:test-infra-cluster-6ef80144-master-0,system:node:test-infra-cluster-aabbccdd-master-0", recert_cmd)
        kubeconfig = ct.KUBECONFIG_DIR / "aabbccdd.kubeconfig"
        kubeconfig.unlink(missing_ok=True)


    @patch("time.sleep")
    @patch.object(ct, "write_remote_file")
    @patch.object(ct, "add_dns_entry")
    def test_identity_mismatch_aborts_boot(self, *_):
        mock_co = self._MOCK_CO_JSON
        mock_nodes = self._MOCK_NODES_JSON
        def ssh_wrong_identity(cmd, check=True):
            self.calls.append((cmd, check))
            r = MagicMock()
            r.returncode = 0
            if "ingress-cn" in cmd:
                r.stdout = "fake-ingress-cn"
            elif "infrastructure cluster" in cmd:
                r.stdout = "https://api.test-infra-cluster-6ef80144.redhat.com:6443"
            elif "get co -o json" in cmd:
                r.stdout = mock_co
            elif "get nodes -o json" in cmd:
                r.stdout = mock_nodes
            else:
                r.stdout = "ok"
            r.stderr = ""
            return r

        with patch.object(ct, "ssh_baremetal", side_effect=ssh_wrong_identity):
            with self.assertRaises(SystemExit) as ctx:
                ct.cmd_boot(self._boot_args())

        self.assertIn("IDENTITY MISMATCH", str(ctx.exception))

    @patch("time.sleep")
    @patch.object(ct, "write_remote_file")
    @patch.object(ct, "add_dns_entry")
    def test_node_not_ready_prints_diagnostics(self, *_):
        mock_nodes_not_ready = json.dumps({"items": [
            {"metadata": {"name": "test-node"}, "status": {"conditions": [
                {"type": "Ready", "status": "False", "reason": "KubeletNotReady", "message": "container runtime not ready"},
            ]}}
        ]})
        def ssh_node_not_ready(cmd, check=True):
            self.calls.append((cmd, check))
            r = MagicMock()
            r.returncode = 0
            if "ingress-cn" in cmd:
                r.stdout = "fake-ingress-cn"
            elif "get nodes -o json" in cmd:
                r.stdout = mock_nodes_not_ready
            elif "get nodes -o wide" in cmd:
                r.stdout = "NAME   STATUS     ROLES   AGE   VERSION\ntest   NotReady   master  1m    v1.32"
            else:
                r.stdout = "ok"
            r.stderr = ""
            return r

        with patch.object(ct, "ssh_baremetal", side_effect=ssh_node_not_ready):
            with self.assertRaises(SystemExit) as ctx:
                ct.cmd_boot(self._boot_args())
        self.assertIn("Node not Ready", str(ctx.exception))
        self.assertIn("NotReady", str(ctx.exception))

    @patch("time.sleep")
    @patch.object(ct, "write_remote_file")
    @patch.object(ct, "add_dns_entry")
    def test_unhealthy_operators_prints_which_ones(self, *_):
        mock_co_bad = json.dumps({"items": [
            {"metadata": {"name": "authentication"}, "status": {"conditions": [
                {"type": "Available", "status": "False", "message": "OAuthServerDown"},
                {"type": "Degraded", "status": "True", "message": "OAuth route unreachable"},
            ]}},
            {"metadata": {"name": "dns"}, "status": {"conditions": [
                {"type": "Available", "status": "True"},
                {"type": "Degraded", "status": "False"},
            ]}},
        ]})
        mock_nodes = self._MOCK_NODES_JSON
        def ssh_co_unhealthy(cmd, check=True):
            self.calls.append((cmd, check))
            r = MagicMock()
            r.returncode = 0
            if "ingress-cn" in cmd:
                r.stdout = "fake-ingress-cn"
            elif "get co -o json" in cmd:
                r.stdout = mock_co_bad
            elif "get nodes -o json" in cmd:
                r.stdout = mock_nodes
            else:
                r.stdout = "ok"
            r.stderr = ""
            return r

        with patch.object(ct, "ssh_baremetal", side_effect=ssh_co_unhealthy):
            with self.assertRaises(SystemExit) as ctx:
                ct.cmd_boot(self._boot_args())
        self.assertIn("Operators not healthy", str(ctx.exception))
        self.assertIn("authentication", str(ctx.exception))
        self.assertNotIn("dns", str(ctx.exception))

    @patch("time.sleep")
    @patch.object(ct, "write_remote_file")
    @patch.object(ct, "remove_dns_entry")
    @patch.object(ct, "remove_haproxy_clone")
    @patch.object(ct, "add_dns_entry")
    def test_kubeconfig_available_before_operator_check(self, *_):
        call_order = []
        mock_co = self._MOCK_CO_JSON
        mock_nodes = self._MOCK_NODES_JSON
        def ssh_tracking(cmd, check=True):
            self.calls.append((cmd, check))
            if "lb-ext.kubeconfig" in cmd and "cat" in cmd:
                call_order.append("kubeconfig_extract")
            elif "get co -o json" in cmd:
                call_order.append("operator_check")
            r = MagicMock()
            r.returncode = 0
            if "ingress-cn" in cmd:
                r.stdout = "fake-ingress-cn"
            elif "infrastructure cluster" in cmd:
                r.stdout = "https://api.test-infra-cluster-aabbccdd.redhat.com:6443"
            elif "get co -o json" in cmd:
                r.stdout = mock_co
            elif "get nodes -o json" in cmd:
                r.stdout = mock_nodes
            else:
                r.stdout = "ok"
            r.stderr = ""
            return r

        with patch.object(ct, "ssh_baremetal", side_effect=ssh_tracking):
            ct.cmd_boot(self._boot_args())

        self.assertIn("kubeconfig_extract", call_order)
        self.assertIn("operator_check", call_order)
        kc_idx = call_order.index("kubeconfig_extract")
        co_idx = call_order.index("operator_check")
        self.assertLess(kc_idx, co_idx)
        state = ct.load_state()
        self.assertIn("aabbccdd", state["clones"])
        kubeconfig = ct.KUBECONFIG_DIR / "aabbccdd.kubeconfig"
        self.assertTrue(kubeconfig.exists())
        kubeconfig.unlink(missing_ok=True)


class TestDetectSourceVM(unittest.TestCase):
    def test_parse_dominfo(self):
        dominfo = """Id:             90
Name:           test-infra-cluster-6ef80144-master-0
UUID:           bd3a9dd8-cc6b-4aae-8ba0-ca4b1c6b238d
OS Type:        hvm
State:          running
CPU(s):         16
Max memory:     67108864 KiB
Used memory:    67108864 KiB"""
        result = ct.parse_dominfo(dominfo)
        self.assertEqual(result["vcpus"], 16)
        self.assertEqual(result["memory_kib"], 67108864)

    @patch.object(ct, "ssh_baremetal")
    def test_parse_disk_paths(self, mock_ssh):
        mock_ssh.return_value = MagicMock(stdout="/resolved/pool/disk-0\n")
        dumpxml = """<domain type='kvm'>
          <devices>
            <disk type='volume' device='disk'>
              <source pool='test-pool' volume='disk-0'/>
              <target dev='sda' bus='scsi'/>
            </disk>
            <disk type='file' device='cdrom'>
              <source file='/tmp/installer.iso'/>
              <target dev='vdz' bus='scsi'/>
              <readonly/>
            </disk>
            <disk type='file' device='disk'>
              <source file='/data/extra-disk.qcow2'/>
              <target dev='sdb' bus='scsi'/>
            </disk>
          </devices>
        </domain>"""
        disks = ct.parse_disk_paths(dumpxml, "test-pool")
        self.assertEqual(len(disks), 2)
        self.assertEqual(disks[0]["target"], "sda")
        self.assertEqual(disks[0]["path"], "/resolved/pool/disk-0")
        self.assertEqual(disks[1]["target"], "sdb")
        self.assertEqual(disks[1]["path"], "/data/extra-disk.qcow2")

    def test_parse_subnet(self):
        net_xml = """<network>
          <ip family='ipv4' address='192.168.135.1' prefix='24'>
            <dhcp>
              <host mac='02:00:00:02:4D:52' ip='192.168.135.10'/>
            </dhcp>
          </ip>
        </network>"""
        subnet = ct.parse_subnet(net_xml)
        self.assertEqual(subnet, 135)


if __name__ == "__main__":
    unittest.main()

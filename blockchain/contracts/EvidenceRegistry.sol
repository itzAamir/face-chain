// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

contract EvidenceRegistry {
    struct EvidenceRecord {
        address submitter;
        uint64 timestamp;
    }

    mapping(bytes32 digest => EvidenceRecord record) public records;

    error EmptyDigest();
    error EvidenceAlreadyExists(bytes32 digest);

    event EvidenceAttested(
        bytes32 indexed digest,
        address indexed submitter,
        uint64 timestamp
    );

    function attest(bytes32 digest) external {
        if (digest == bytes32(0)) revert EmptyDigest();
        if (records[digest].timestamp != 0) revert EvidenceAlreadyExists(digest);

        uint64 timestamp = uint64(block.timestamp);
        records[digest] = EvidenceRecord({
            submitter: msg.sender,
            timestamp: timestamp
        });

        emit EvidenceAttested(digest, msg.sender, timestamp);
    }

    function verify(bytes32 digest)
        external
        view
        returns (bool exists, address submitter, uint64 timestamp)
    {
        EvidenceRecord memory record = records[digest];
        return (record.timestamp != 0, record.submitter, record.timestamp);
    }
}

